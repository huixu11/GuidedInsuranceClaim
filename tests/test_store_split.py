"""The SQL stores keep a conversation as a small row plus its messages, traces and audit events as
rows of their own, written once each: what that layout promises, and what happens to each part
when the conversation expires or is evicted.

White-box where the promise is about rows: the checks read the store's own tables.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid

import pytest

from insurance_claims.state import SessionState
from insurance_claims.store import ConversationBusy, SessionNotFound, SqliteStore, StoreError

SQL = pytest.mark.parametrize("store_kind", ["sqlite", "postgres"], indirect=True)


def new_state() -> SessionState:
    state = SessionState(session_id=uuid.uuid4().hex)
    state.messages.append({"role": "assistant", "text": "Hello", "turn": 0, "id": "m-0"})
    state.audit("conversation_started", consent_scenario="default", phase="VERIFY_ID")
    return state


def rows(store, sql: str, params: dict | None = None) -> list:
    """A query on the store's own database."""
    return [tuple(row) for row in store._session(lambda ex: ex.run(sql, params or {}).rows)]


def execute(store, sql: str, params: dict | None = None) -> None:
    store._session(lambda ex: ex.run(sql, params or {}, write=True))


def count(store, table: str, session_id: str) -> int:
    column = "id" if table == "conversations" else "session_id"
    return rows(store, f"SELECT COUNT(*) FROM {table} WHERE {column} = :id", {"id": session_id})[0][0]


def forbid_rewrites(store, kind: str) -> None:
    """Make any UPDATE or DELETE of a written-once row fail loudly."""
    tables = ("messages", "traces", "audit_events")
    if kind == "sqlite":
        for table in tables:
            for action in ("UPDATE", "DELETE"):
                execute(
                    store,
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} rows are written once'); END",
                )
    else:
        execute(
            store,
            "CREATE FUNCTION written_once() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'rows are written once'; END $$",
        )
        for table in tables:
            execute(
                store,
                f"CREATE TRIGGER {table}_written_once BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION written_once()",
            )


def one_turn(store, session_id: str, n: int) -> None:
    with store.turn(session_id) as t:
        t.state.turn = n
        t.state.messages.append({"role": "user", "text": f"message {n}", "turn": n, "id": f"u-{n}"})
        t.state.messages.append({"role": "assistant", "text": f"reply {n}", "turn": n, "id": f"a-{n}"})
        t.state.traces.append({"turn": n, "latency_ms": n})
        t.state.audit("phase_changed", **{"from": "VERIFY_ID", "to": "VERIFY_ID", "reason": f"turn {n}"})
        t.commit()


# ---------------------------------------------------------------------------
# Written once, appended to
# ---------------------------------------------------------------------------
@SQL
def test_each_entry_is_a_row_written_once_and_earlier_rows_stay_as_they_were(store_factory, store_kind):
    store = store_factory()
    forbid_rewrites(store, store_kind)
    s = new_state()
    store.create(s)
    one_turn(store, s.session_id, 1)
    before = rows(store, "SELECT seq, data FROM messages WHERE session_id = :id ORDER BY seq", {"id": s.session_id})
    xmin_before = (
        rows(store, "SELECT seq, xmin::text FROM messages WHERE session_id = :id ORDER BY seq", {"id": s.session_id})
        if store_kind == "postgres"
        else None
    )
    for n in range(2, 13):  # past 10 rows, where ordering by text would go wrong
        one_turn(store, s.session_id, n)

    back = store.read(s.session_id)
    assert back.turn == 12 and len(back.messages) == 1 + 2 * 12 and len(back.traces) == 12 and len(back.events) == 13
    assert [m["text"] for m in back.messages[1:4]] == ["message 1", "reply 1", "message 2"]
    assert back.messages[-1]["text"] == "reply 12" and [e["seq"] for e in back.events] == list(range(13))
    for table, expected in (("messages", 25), ("traces", 12), ("audit_events", 13), ("conversations", 1)):
        assert count(store, table, s.session_id) == expected, table
    # The rows written by the first turn are exactly as they were written...
    after = rows(store, "SELECT seq, data FROM messages WHERE session_id = :id AND seq < 3 ORDER BY seq", {"id": s.session_id})
    assert after == before
    if store_kind == "postgres":  # ...not even rewritten with the same content
        assert rows(
            store, "SELECT seq, xmin::text FROM messages WHERE session_id = :id AND seq < 3 ORDER BY seq", {"id": s.session_id}
        ) == xmin_before
    # The conversation's own row holds the rest of the state, without the lists.
    (state_text,) = rows(store, "SELECT state FROM conversations WHERE id = :id", {"id": s.session_id})[0]
    record = json.loads(state_text)
    assert record["turn"] == 12 and not {"messages", "traces", "events"} & set(record)
    # Copies for querying.
    assert rows(store, "SELECT turn, phase, status FROM conversations WHERE id = :id", {"id": s.session_id}) == [
        (12, "VERIFY_ID", "active")
    ]
    assert rows(
        store, "SELECT role, message_id, turn FROM messages WHERE session_id = :id AND seq = 1", {"id": s.session_id}
    ) == [("user", "u-1", 1)]
    assert rows(
        store, "SELECT type, turn FROM audit_events WHERE session_id = :id AND seq = 1", {"id": s.session_id}
    ) == [("phase_changed", 1)]


@SQL
def test_many_threads_on_many_conversations_only_ever_append(store_factory, store_kind):
    store = store_factory(wait_seconds=30)
    forbid_rewrites(store, store_kind)
    states = [new_state() for _ in range(4)]
    for s in states:
        store.create(s)
    errors: list[BaseException] = []

    def work(session_id: str) -> None:
        try:
            for n in range(1, 11):
                one_turn(store, session_id, n)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s.session_id,)) for s in states for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    for s in states:
        back = store.read(s.session_id)
        assert len(back.messages) == 41 and [e["seq"] for e in back.events] == list(range(21))
        assert count(store, "messages", s.session_id) == 41


def test_a_shorter_or_rewritten_list_is_refused_and_nothing_is_written(store_factory):
    store = store_factory()
    s = new_state()
    store.create(s)
    one_turn(store, s.session_id, 1)
    before = store.read(s.session_id)

    def attempt(change) -> None:
        with pytest.raises(StoreError, match="append-only|written once|shorter"):
            with store.turn(s.session_id) as t:
                t.state.turn = 99
                t.state.messages.append({"role": "user", "text": "new", "turn": 99})
                change(t.state)
                t.commit()
        assert store.read(s.session_id) == before

    attempt(lambda state: state.messages.pop(0))
    attempt(lambda state: state.messages[0].update(text="rewritten"))
    attempt(lambda state: state.traces.clear())
    attempt(lambda state: state.events[0]["data"].update(phase="X"))
    with store.turn(s.session_id, wait=0) as t:  # and the conversation was released each time
        t.commit()


def test_audit_events_of_any_shape_come_back_as_they_were(store_factory):
    store = store_factory()
    s = new_state()
    odd = [
        {"seq": 1, "turn": 0, "at": 1, "type": "int_time", "data": {}},  # an int time: kept as an int
        {"seq": 2, "turn": 0, "at": 1.5, "type": "extra", "data": {}, "note": "an extra key"},
        {"type": "no_seq"},
        {"seq": 4, "turn": None, "at": 2.5, "type": "t", "data": {"__event__": "looks like the wrapper"}},
        "not even a dict",
        {"seq": 6, "turn": 0, "at": 2.5, "type": "x" * 300, "data": [1, 2]},
    ]
    s.events.extend(odd)
    store.create(s)
    assert store.read(s.session_id).events == s.events
    assert store.audit(s.session_id) == s.events
    with store.turn(s.session_id) as t:
        t.state.audit("phase_changed", **{"from": "A", "to": "B"})
        t.commit()
    assert store.audit(s.session_id)[:-1] == s.events and store.audit(s.session_id)[-1]["type"] == "phase_changed"


def test_audit_event_times_come_back_exactly(store_factory):
    """Any float `at` is kept to the last bit (and -0.0 keeps its sign), however it is stored."""
    store = store_factory()
    s = new_state()
    times = [1795451102.585, 1795451102.5851234, 0.1 + 0.2, 5e-324, 1.7976931348623157e308, -0.0, 0.0, 1e16 + 2]
    for i, at in enumerate(times, start=len(s.events)):
        s.events.append({"seq": i, "turn": 0, "at": at, "type": "timed", "data": {"i": i}})
    store.create(s)
    with store.turn(s.session_id) as t:
        t.state.events.append({"seq": len(t.state.events), "turn": 1, "at": 1795451103.123456789, "type": "timed", "data": {}})
        t.commit()
    expected = [e["at"] for e in s.events] + [1795451103.123456789]
    for events in (store.read(s.session_id).events, store.audit(s.session_id)):
        got = [e["at"] for e in events]
        assert got == expected and [math.copysign(1, x) for x in got] == [math.copysign(1, x) for x in expected]


@SQL
def test_rows_missing_from_the_middle_are_reported_not_misread(store_factory):
    store = store_factory()
    s = new_state()
    store.create(s)
    one_turn(store, s.session_id, 1)
    execute(store, "DELETE FROM messages WHERE session_id = :id AND seq = 1", {"id": s.session_id})
    with pytest.raises(StoreError, match="numbered"):
        store.read(s.session_id)


@SQL
@pytest.mark.parametrize("table", ["messages", "traces", "audit_events"])
@pytest.mark.parametrize("garbage", ['{"a": 1}, {"b": 2}', "not json", ""])
def test_a_row_that_is_not_one_json_value_is_reported_not_misread(store_factory, table, garbage):
    """(The rows of a list are parsed together, so two values in one row must not pass as two rows.)"""
    store = store_factory()
    s = new_state()
    store.create(s)
    one_turn(store, s.session_id, 1)
    execute(store, f"UPDATE {table} SET data = :garbage WHERE session_id = :id AND seq = 0",
            {"id": s.session_id, "garbage": garbage})
    with pytest.raises(StoreError, match="cannot read"):
        store.read(s.session_id)
    with pytest.raises(StoreError):
        with store.turn(s.session_id):
            pass
    if table == "audit_events":
        with pytest.raises(StoreError, match="cannot be read"):
            store.audit(s.session_id)


@SQL
def test_a_read_never_writes(store_factory):
    store = store_factory()
    s = new_state()
    store.create(s)
    one_turn(store, s.session_id, 1)
    snapshot = rows(store, "SELECT * FROM conversations WHERE id = :id", {"id": s.session_id})
    store.test_clock.advance(50)
    for _ in range(3):
        store.read(s.session_id)
        store.audit(s.session_id)
        store.effects(s.session_id)
        store.stats()
    assert rows(store, "SELECT * FROM conversations WHERE id = :id", {"id": s.session_id}) == snapshot


# ---------------------------------------------------------------------------
# Expiry, eviction and retention
# ---------------------------------------------------------------------------
def _with_three_effects(store, session_id: str) -> dict[str, int]:
    """Effects in each state: 'sent', 'dead' and (due in 10 000 s) 'pending'."""
    with store.turn(session_id) as t:
        t.state.messages.append({"role": "user", "text": "please e-mail me", "turn": 1})
        t.state.traces.append({"turn": 1})
        t.state.audit("email_queued", recipient="caller", case_ids=["CL-1"], summary_source="template")
        t.commit(
            effects=[
                {"kind": "email", "key": key, "payload": {"n": i}}
                for i, key in enumerate(("to-send", "to-fail", "to-retry"))
            ]
        )
    claimed = {c["key"]: c for c in store.claim_effects(limit=10)}
    assert store.finish_effect(claimed["to-send"]["id"], claimed["to-send"]["token"], ok=True)
    assert store.finish_effect(claimed["to-fail"]["id"], claimed["to-fail"]["token"], ok=False, error="no")
    assert store.finish_effect(
        claimed["to-retry"]["id"], claimed["to-retry"]["token"], ok=False, error="later", retry_in=10_000
    )
    return {key: c["id"] for key, c in claimed.items()}


@SQL
def test_expiry_removes_the_conversation_but_keeps_its_audit_trail_until_the_retention(store_factory):
    store = store_factory(ttl_seconds=100, audit_retention_seconds=50_000)
    clock = store.test_clock
    s = new_state()
    store.create(s)
    ids = _with_three_effects(store, s.session_id)
    trail = store.audit(s.session_id)
    assert [e["type"] for e in trail] == ["conversation_started", "email_queued"]

    clock.advance(101)
    swept = store.sweep()
    assert swept["conversations"] == 1 and store.stats()["expired_since_start"] == 1
    with pytest.raises(SessionNotFound):
        store.read(s.session_id)
    for table in ("conversations", "messages", "traces"):
        assert count(store, table, s.session_id) == 0, table
    # Delivered and dead effects went with it; the one still to deliver stays, and so does the trail.
    assert [(e["id"], e["status"]) for e in store.effects(s.session_id)] == [(ids["to-retry"], "pending")]
    assert store.audit(s.session_id) == trail

    clock.advance(10_000)  # the remaining effect is delivered after all
    (claim,) = store.claim_effects()
    assert claim["key"] == "to-retry" and claim["attempts"] == 2
    assert store.finish_effect(claim["id"], claim["token"], ok=True)
    assert store.sweep()["effects"] == 1  # delivered, and its conversation is gone
    assert store.effects(s.session_id) == [] and store.audit(s.session_id) == trail

    clock.advance(50_000)
    assert store.sweep()["audit_events"] == 2
    assert store.audit(s.session_id) == [] and count(store, "audit_events", s.session_id) == 0


@SQL
def test_an_undelivered_effect_of_a_gone_conversation_is_dropped_after_the_retention(store_factory):
    store = store_factory(ttl_seconds=100, audit_retention_seconds=50_000)
    clock = store.test_clock
    s = new_state()
    store.create(s)
    _with_three_effects(store, s.session_id)
    clock.advance(101)
    store.sweep()
    clock.advance(49_000)
    store.sweep()
    assert [e["key"] for e in store.effects(s.session_id)] == ["to-retry"]
    clock.advance(1_001)
    assert store.sweep()["effects"] == 1 and store.effects(s.session_id) == []


@SQL
def test_eviction_follows_the_same_rules_as_expiry(store_factory):
    store = store_factory(max_sessions=1)
    clock = store.test_clock
    a, b = new_state(), new_state()
    store.create(a)
    ids = _with_three_effects(store, a.session_id)
    clock.advance(1)
    store.create(b)  # a is evicted
    with pytest.raises(SessionNotFound):
        store.read(a.session_id)
    for table in ("conversations", "messages", "traces"):
        assert count(store, table, a.session_id) == 0, table
    assert [e["id"] for e in store.effects(a.session_id)] == [ids["to-retry"]]
    assert [e["type"] for e in store.audit(a.session_id)] == ["conversation_started", "email_queued"]
    assert store.stats()["evicted_since_start"] == 1


@SQL
def test_the_retention_never_touches_a_live_conversation(store_factory):
    store = store_factory(ttl_seconds=100_000, audit_retention_seconds=10)
    s = new_state()
    store.create(s)
    store.test_clock.advance(1_000)
    store.sweep()
    assert len(store.audit(s.session_id)) == 1 and len(store.read(s.session_id).events) == 1


@SQL
def test_the_conversation_count_stays_exact(store_factory):
    store = store_factory(max_sessions=5, ttl_seconds=100)
    clock = store.test_clock
    for _ in range(9):  # 4 evictions
        store.create(new_state())
        clock.advance(1)
    # The five left were last used at +4 .. +8 s; at +106.5 s the three oldest are past the TTL
    # (half-second margins: PostgreSQL's clock moves on by the time a statement takes).
    clock.advance(97.5)
    assert store.sweep()["conversations"] == 3
    actual = rows(store, "SELECT COUNT(*) FROM conversations")[0][0]
    counted = rows(store, store._count_sql)[0][0]
    assert counted == actual == store.stats()["sessions_held"] == 2


def test_creating_and_taking_turns_never_counts_rows(tmp_path):
    statements: list[str] = []
    store = SqliteStore(tmp_path / "s.db", background=False)
    try:
        store._conn().set_trace_callback(statements.append)
        s = new_state()
        store.create(s)
        one_turn(store, s.session_id, 1)
        store.read(s.session_id)
    finally:
        store.close()
    assert statements and not [sql for sql in statements if "COUNT(" in sql.upper()]


def test_a_sqlite_turn_commits_in_one_statement(tmp_path):
    """SQLite's write lock (one for every process on the file) is held by one statement only, not
    across a BEGIN ... COMMIT during which the thread waits for the GIL between statements."""
    statements: list[str] = []
    store = SqliteStore(tmp_path / "s.db", background=False)
    try:
        s = new_state()
        store.create(s)
        with store.turn(s.session_id) as t:
            t.state.turn = 1
            t.state.messages.append({"role": "user", "text": "hi", "turn": 1, "id": "u-1"})
            t.state.traces.append({"turn": 1, "latency_ms": 1})
            t.state.audit("phase_changed", **{"from": "VERIFY_ID", "to": "VERIFY_ID", "reason": "hi"})
            store._conn().set_trace_callback(statements.append)
            t.commit(effects=[{"kind": "email", "key": "summary", "payload": {"n": 1}}])
            store._conn().set_trace_callback(None)
        back = store.read(s.session_id)
    finally:
        store.close()
    assert not [sql for sql in statements if sql.lstrip().upper().startswith(("BEGIN", "COMMIT", "SAVEPOINT"))]
    shared = {sql for sql in statements if "temp.sop_staged" not in sql}  # (the rest is this connection's own)
    assert len(shared) == 1 and next(iter(shared)).startswith("INSERT INTO temp.sop_turn_commit")
    assert back.turn == 1 and back.messages[-1]["text"] == "hi" and back.traces == [{"turn": 1, "latency_ms": 1}]
    assert [e["type"] for e in back.events] == ["conversation_started", "phase_changed"]


def test_a_sqlite_turn_with_more_rows_than_one_statement_takes_commits_whole(tmp_path):
    store = SqliteStore(tmp_path / "s.db", background=False)
    try:
        s = new_state()
        store.create(s)
        with store.turn(s.session_id) as t:
            t.state.turn = 1
            for n in range(store.MAX_ROWS_PER_STATEMENT + 5):
                t.state.audit("tool_called", n=n)
            t.state.messages.append({"role": "user", "text": "many", "turn": 1, "id": "u-1"})
            t.commit(effects=[{"kind": "email", "key": "summary", "payload": {}}])
        back = store.read(s.session_id)
        assert len(back.events) == 1 + store.MAX_ROWS_PER_STATEMENT + 5 and back.events[-1]["data"] == {"n": 1004}
        assert back.messages[-1]["text"] == "many" and [e["key"] for e in store.effects(s.session_id)] == ["summary"]
        one_turn(store, s.session_id, 2)  # and the usual one-statement commit goes on from there
        assert store.read(s.session_id).turn == 2
    finally:
        store.close()


def test_a_refused_sqlite_commit_leaves_nothing_behind_for_the_next_one(tmp_path):
    """The rows a refused commit staged on a connection never reach the next commit made on it."""
    a = SqliteStore(tmp_path / "s.db", lease_seconds=5, background=False)
    b = SqliteStore(tmp_path / "s.db", lease_seconds=5, background=False)
    try:
        lost, other = new_state(), new_state()
        a.create(lost)
        a.create(other)
        late = a.turn(lost.session_id, wait=0)
        stuck = late.__enter__()
        execute(a, "UPDATE conversations SET lease_until = 0 WHERE id = :id", {"id": lost.session_id})  # lapsed
        one_turn(b, lost.session_id, 1)  # another turn took it and committed
        stuck.state.messages.append({"role": "user", "text": "late", "turn": 1, "id": "late"})
        stuck.state.audit("phase_changed", **{"from": "VERIFY_ID", "to": "VERIFY_ID", "reason": "late"})
        with pytest.raises(ConversationBusy):
            stuck.commit(effects=[{"kind": "email", "key": "late", "payload": {}}])
        late.__exit__(None, None, None)
        one_turn(a, other.session_id, 1)  # the same thread, so the same connection
        with a.turn(other.session_id) as t:
            t.commit(effects=[{"kind": "email", "key": "on-time", "payload": {}}])
        texts = [m["text"] for m in a.read(other.session_id).messages]
        assert texts == ["Hello", "message 1", "reply 1"]
        assert [m["text"] for m in a.read(lost.session_id).messages] == ["Hello", "message 1", "reply 1"]
        assert [e["key"] for e in a.effects(other.session_id)] == ["on-time"] and a.effects(lost.session_id) == []
        assert rows(a, "SELECT COUNT(*) FROM temp.sop_staged") == [(0,)]
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------
def test_the_previous_versions_sessions_table_is_left_alone(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, version INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0);
            CREATE TABLE store_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
            INSERT INTO store_meta VALUES ('sessions', 1), ('last_sweep', 0);
            INSERT INTO sessions VALUES ('old-one', '{"format": 1}', 3, 1, 2, NULL, 0);
            """
        )
    conn.close()
    store = SqliteStore(path, background=False)
    try:
        s = new_state()
        store.create(s)
        assert store.read(s.session_id) == s
        with pytest.raises(SessionNotFound):
            store.read("old-one")  # not migrated
    finally:
        store.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id, state, version FROM sessions").fetchall() == [("old-one", '{"format": 1}', 3)]
        meta = dict(conn.execute("SELECT key, value FROM store_meta").fetchall())
    conn.close()
    assert meta["sessions"] == 1 and meta["schema_version"] == 2 and meta["conversation_count"] == 1


@SQL
def test_a_database_written_by_a_newer_version_is_refused(store_factory):
    store = store_factory()
    execute(store, "UPDATE store_meta SET value = 3 WHERE key = 'schema_version'")
    with pytest.raises(StoreError, match="newer version"):
        store_factory()
