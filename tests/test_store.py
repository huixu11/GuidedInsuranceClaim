"""Session stores: one contract, three implementations (MemoryStore, SqliteStore, PostgresStore).

Every test in the first part runs against all three stores. The second part covers what only a
store shared between processes has (SQLite and PostgreSQL): two store objects on one file or
database, a lease that lapses after a crash, and a second operating-system process holding a
conversation. PostgreSQL comes from SOP_TEST_POSTGRES_URL or an embedded server (conftest.py);
without either, its cases are skipped with the reason.

More: test_store_split.py (rows of their own, deletion and retention), test_store_effects.py (the
outbox), test_store_leases.py (renewal and its limit), test_store_crypto.py (encryption at rest),
test_store_postgres.py (outages, the pool, the password).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from insurance_claims.state import MemoryItem, Phase, SessionState
from insurance_claims.store import (
    ConversationBusy,
    MemoryStore,
    PostgresStore,
    SessionNotFound,
    SqliteStore,
    StoreError,
    open_store,
    parse_store_spec,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
SHARED = pytest.mark.parametrize("store_kind", ["sqlite", "postgres"], indirect=True)


class FakeClock:
    """The stores' notion of time, moved by hand (waiting for a busy conversation still uses
    real time)."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def new_state() -> SessionState:
    state = SessionState(session_id=uuid.uuid4().hex)
    state.messages.append({"role": "assistant", "text": "Hello", "turn": 0})
    return state


def rich_state() -> SessionState:
    """Every field set to something other than its default, including the awkward types."""
    s = new_state()
    s.consent_scenario = "timeout"
    s.turn = 7
    s.phase = Phase.POST_PROCESS
    s.status = "completed"
    s.phase_history = [{"turn": 0, "from": None, "to": "VERIFY_ID", "reason": "session_start"},
                       {"turn": 2, "from": "VERIFY_ID", "to": "RESOLVE_INTENT", "reason": "identity verified"}]
    s.identity_claims = {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}
    s.caller_role = "representative"
    s.rep_name, s.rep_relationship, s.rep_on_file = "David Chen", "son", True
    s.verified, s.party_id, s.candidate_party_id = True, "P9", "P9"
    s.field_status = {"full_name": "matched", "dob": "matched", "phone": "not_provided",
                      "email": "mismatched", "id_last4": "matched"}
    s.policy_status = "matched"
    s.failed_attempts = 1
    s.seen_mismatches = ["email=x@example.org"]
    s.declined_fields = ["phone"]
    s.consent_required, s.consent_status, s.consent_polls = True, "approved", 2
    s.consent_party_id, s.consent_rep = "P9", "chen david"
    s.consent_ledger = {"P7|chen david": {"status": "timed_out", "polls": 3}}
    s.intent = MemoryItem("denial_question", 1, "VERIFY_ID")
    s.hints = {"status": MemoryItem("denied", 1, "VERIFY_ID"), "case_type": MemoryItem("healthcare", 2, "RESOLVE_INTENT")}
    s.notes = [MemoryItem("prefers mornings", 3, "PROCESS_CASE")]
    s.email_pref = MemoryItem("send", 4, "PROCESS_CASE")
    s.claims_on_file = 2
    s.selected_case_id, s.selected_case_brief, s.case_intent = "CL-2048", "CL-2048 (healthcare claim)", "denial_question"
    s.candidates, s.cases_discussed, s.clarify_attempts = ["CL-2048", "CL-2102"], ["CL-2048"], 1
    s.oos_consecutive, s.oos_total = 1, 2
    s.emotion_label, s.emotion_intensity, s.elevated_streak = "frustrated", 2, 1
    s.refusal_count, s.human_offer_pending, s.guard_blocks = 1, True, 1
    s.escalation = {"reason": "caller_requested", "turn": 5, "handoff_summary": "Handoff\nReason: x"}
    s.email_offered, s.email_decision, s.email_reasks = True, "send", 1
    s.outbox = [{"to": "m***@email.com", "subject": "Summary", "body": "Hello — ünïcödé ✓", "sent_turn": 7}]
    s.messages.append({"role": "user", "text": "Ça va? 🙂 مرحبا \u202e\x00\x1b[0m", "turn": 1})
    s.traces = [{"turn": 1, "phase_before": "VERIFY_ID", "phase_after": "PROCESS_CASE", "latency_ms": 3,
                 "events": [{"kind": "llm", "label": "understand", "detail": {"input_tokens": 12, "x": [1, 2.5, None]}}]}]
    s.grounding = ['{"case_id": "CL-2048"}']
    s.audit("identity_verified", party_id="P9", matched_fields=["full_name", "dob", "id_last4"])
    s.audit("phase_changed", **{"from": "VERIFY_ID", "to": "RESOLVE_INTENT", "reason": "identity verified"})
    return s


@pytest.fixture()
def kind(store_kind):
    return store_kind


@pytest.fixture()
def make_store(store_factory):
    """make_store(clock=None, **options) -> a store of the parametrised kind (memory, sqlite,
    postgres); the SQL ones of one test share one file or database (conftest.store_factory)."""
    return store_factory


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------
def test_record_is_lossless_json_with_every_field_set():
    s = rich_state()
    text = json.dumps(s.to_record(), allow_nan=False)
    back = SessionState.from_record(json.loads(text))
    assert back == s
    assert back.phase is Phase.POST_PROCESS and isinstance(back.intent, MemoryItem)
    assert back.hints["status"] == MemoryItem("denied", 1, "VERIFY_ID")
    assert back.to_view(_Settings()) == s.to_view(_Settings())


def test_record_defaults_and_none_survive():
    s = new_state()
    assert s.intent is None and s.email_pref is None and s.escalation is None
    assert SessionState.from_record(json.loads(json.dumps(s.to_record()))) == s


def test_record_from_an_older_or_newer_version_of_the_code():
    record = new_state().to_record()
    # A field this version does not know (written by a newer one, mid-deployment) is carried
    # along and written back unchanged, not refused and not lost.
    newer = {**record, "added_by_a_newer_version": {"n": 1}}
    loaded = SessionState.from_record(newer)
    assert loaded.unknown_fields == {"added_by_a_newer_version": {"n": 1}}
    assert loaded.to_record() == newer
    assert "unknown_fields" not in loaded.to_record()
    # A different format marks a change old code must not guess at.
    with pytest.raises(ValueError, match="format"):
        SessionState.from_record({**record, "format": 99})
    # A field added later (absent from an older record) takes its default.
    record.pop("guard_blocks")
    assert SessionState.from_record(record).guard_blocks == 0


def test_audit_events_are_numbered_and_survive_the_record():
    s = new_state()
    s.turn = 3
    s.audit("identity_verified", party_id="P-1", matched_fields=["full_name", "dob", "id_last4"])
    s.audit("phase_changed", **{"from": "VERIFY_ID", "to": "RESOLVE_INTENT"})
    assert [e["seq"] for e in s.events] == [0, 1] and {e["turn"] for e in s.events} == {3}
    assert s.events[0]["type"] == "identity_verified" and s.events[0]["data"]["party_id"] == "P-1"
    assert SessionState.from_record(json.loads(json.dumps(s.to_record()))) == s


class _Settings:
    required_id_matches = 3
    max_failed_attempts = 3
    out_of_scope_limit = 3
    max_persuasion_attempts = 2


# ---------------------------------------------------------------------------
# The contract, for every store
# ---------------------------------------------------------------------------
def test_create_then_read_gives_an_equal_private_copy(make_store):
    store = make_store()
    s = rich_state()
    store.create(s)
    got = store.read(s.session_id)
    assert got == s and got is not s
    got.turn = 99
    got.messages.append({"role": "user", "text": "not stored", "turn": 99})
    assert store.read(s.session_id) == s
    with pytest.raises(StoreError):
        store.create(s)  # an id exists once


def test_a_turn_commits_and_a_failed_turn_writes_nothing(make_store):
    store = make_store()
    s = new_state()
    store.create(s)
    with store.turn(s.session_id) as t:
        assert t.state == s and t.state is not s
        t.state.turn = 1
        t.state.messages.append({"role": "user", "text": "hi", "turn": 1})
        t.commit()
    assert store.read(s.session_id).turn == 1

    with pytest.raises(RuntimeError):
        with store.turn(s.session_id) as t:
            t.state.turn = 2
            t.state.messages.clear()
            raise RuntimeError("the model call failed")
    after = store.read(s.session_id)
    assert after.turn == 1 and len(after.messages) == 2

    with store.turn(s.session_id) as t:  # leaving without commit() writes nothing either
        t.state.turn = 3
    assert store.read(s.session_id).turn == 1

    with store.turn(s.session_id, wait=0) as t:  # and the conversation is free again at once
        assert t.state.turn == 1
        t.commit()
        with pytest.raises(StoreError):
            t.commit()  # once


def test_a_turn_round_trips_every_field(make_store):
    store = make_store()
    s = new_state()
    store.create(s)
    rich = rich_state()
    with store.turn(s.session_id) as t:
        for f in dataclasses.fields(SessionState):
            if f.name not in ("session_id", "messages"):
                setattr(t.state, f.name, getattr(rich, f.name))
        t.state.messages.extend(rich.messages[1:])
        t.commit()
    expected = dataclasses.replace(rich, session_id=s.session_id)
    assert store.read(s.session_id) == expected
    with store.turn(s.session_id) as t:
        assert t.state == expected


def test_a_read_during_a_turn_sees_the_last_commit(make_store):
    store = make_store()
    s = new_state()
    store.create(s)
    with store.turn(s.session_id) as t:
        t.state.turn = 5
        t.state.messages.append({"role": "user", "text": "in progress", "turn": 5})
        assert store.read(s.session_id).turn == 0
        assert len(store.read(s.session_id).messages) == 1
        t.commit()
    assert store.read(s.session_id).turn == 5


def test_an_unknown_conversation_is_not_found_at_once(make_store):
    store = make_store(wait_seconds=5)
    with pytest.raises(SessionNotFound):
        store.read("does-not-exist")
    started = time.monotonic()
    with pytest.raises(SessionNotFound):
        with store.turn("does-not-exist"):
            pass
    assert time.monotonic() - started < 1  # no waiting for something that does not exist
    assert store.audit("does-not-exist") == [] and store.effects("does-not-exist") == []


def test_idle_conversations_expire_on_read_and_on_turn(make_store):
    store = make_store(ttl_seconds=100)
    clock = store.test_clock
    a, b = new_state(), new_state()
    store.create(a)
    store.create(b)
    clock.advance(60)
    store.read(a.session_id)  # reading is not use: it does not keep a conversation alive
    with store.turn(b.session_id) as t:  # a turn is
        t.commit()
    clock.advance(41)  # a: 101 s since its last use; b: 41 s
    with pytest.raises(SessionNotFound):
        store.read(a.session_id)
    with pytest.raises(SessionNotFound):
        with store.turn(a.session_id):
            pass
    assert store.read(b.session_id) == b
    clock.advance(100)
    with pytest.raises(SessionNotFound):
        with store.turn(b.session_id):
            pass
    assert store.stats()["sessions_held"] == 0


def test_the_cap_evicts_the_least_recently_used(make_store):
    store = make_store(max_sessions=3)
    clock = store.test_clock
    a, b, c, d = (new_state() for _ in range(4))
    for s in (a, b, c):
        store.create(s)
        clock.advance(1)
    with store.turn(a.session_id) as t:  # a is used again, so b is now the least recently used
        t.commit()
    clock.advance(1)
    store.create(d)
    with pytest.raises(SessionNotFound):
        store.read(b.session_id)
    for s in (a, c, d):
        assert store.read(s.session_id).session_id == s.session_id
    stats = store.stats()
    assert stats["sessions_held"] == 3 and stats["max_sessions"] == 3 and stats["evicted_since_start"] == 1


def test_a_conversation_mid_turn_is_not_evicted(make_store):
    store = make_store(max_sessions=2)
    clock = store.test_clock
    a, b, c = (new_state() for _ in range(3))
    store.create(a)
    clock.advance(1)
    with store.turn(a.session_id) as t:  # a is the least recently used of all, but it is in use
        store.create(b)
        clock.advance(1)
        store.create(c)
        t.state.turn = 1
        t.commit()
    assert store.read(a.session_id).turn == 1
    with pytest.raises(SessionNotFound):
        store.read(b.session_id)
    assert store.read(c.session_id) == c


def test_the_memory_store_also_keeps_to_a_byte_budget():
    clock = FakeClock()
    sizes = len(json.dumps(new_state().to_record(), separators=(",", ":")))
    store = MemoryStore(ttl_seconds=100, max_sessions=1000, clock=clock, max_bytes=int(sizes * 3.5))
    a, b, c, d = (new_state() for _ in range(4))
    for s in (a, b, c):
        store.create(s)
        clock.advance(1)
    store.create(d)  # four would not fit: the least recently used goes
    with pytest.raises(SessionNotFound):
        store.read(a.session_id)
    with store.turn(b.session_id) as t:  # a turn that grows its conversation makes room too...
        t.state.messages.append({"role": "user", "text": "x" * sizes, "turn": 1})
        t.commit()
    with pytest.raises(SessionNotFound):
        store.read(c.session_id)  # ...from the least recently used, never from itself
    assert len(store.read(b.session_id).messages) == 2 and store.read(d.session_id) == d
    stats = store.stats()
    assert stats["state_bytes_held"] <= stats["max_state_bytes"] and stats["evicted_since_start"] == 2


def _hold(store, session_id: str, entered: threading.Event, release: threading.Event, turn: int) -> threading.Thread:
    def run() -> None:
        with store.turn(session_id) as t:
            entered.set()
            release.wait(10)
            t.state.turn = turn
            t.commit()

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(10)
    return thread


def test_a_busy_conversation_is_refused_after_a_bounded_wait(make_store):
    store = make_store(wait_seconds=5)
    s = new_state()
    store.create(s)
    entered, release = threading.Event(), threading.Event()
    holder = _hold(store, s.session_id, entered, release, turn=1)
    started = time.monotonic()
    with pytest.raises(ConversationBusy):
        with store.turn(s.session_id, wait=0.3):
            pass
    waited = time.monotonic() - started
    assert 0.25 <= waited < 3, waited
    release.set()
    holder.join(10)
    with store.turn(s.session_id, wait=0) as t:
        assert t.state.turn == 1


def test_a_waiting_turn_goes_ahead_when_the_conversation_is_released(make_store):
    store = make_store()
    s = new_state()
    store.create(s)
    entered, release = threading.Event(), threading.Event()
    holder = _hold(store, s.session_id, entered, release, turn=1)
    threading.Timer(0.2, release.set).start()
    with store.turn(s.session_id, wait=5) as t:
        assert t.state.turn == 1  # it sees the commit it waited for
        t.state.turn = 2
        t.commit()
    holder.join(10)
    assert store.read(s.session_id).turn == 2


def test_no_update_is_lost_when_many_threads_share_one_conversation(make_store):
    store = make_store(wait_seconds=30)
    s = new_state()
    store.create(s)
    threads, per_thread = 6, 20
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(per_thread):
                with store.turn(s.session_id) as t:
                    t.state.turn += 1
                    t.state.messages.append({"role": "user", "text": str(t.state.turn), "turn": t.state.turn})
                    t.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(60)
    assert not errors
    final = store.read(s.session_id)
    assert final.turn == threads * per_thread
    assert [m["text"] for m in final.messages[1:]] == [str(i) for i in range(1, threads * per_thread + 1)]


def test_stats_describe_the_store_without_content(make_store, kind):
    store = make_store()
    s = rich_state()
    store.create(s)
    stats = store.stats()
    assert stats["kind"] == kind and stats["sessions_held"] == 1 and stats["turns_in_progress"] == 0
    assert stats["shared_between_processes"] is (kind != "memory")
    assert stats["effects"] == {"pending": 0, "sent": 0, "dead": 0}
    blob = json.dumps(stats)
    assert s.session_id not in blob and "Margaret" not in blob
    store.ping()
    assert kind in store.describe()


# ---------------------------------------------------------------------------
# SQLite and PostgreSQL: shared between store objects and processes
# ---------------------------------------------------------------------------
@pytest.fixture()
def shared_pair(store_factory):
    clock = FakeClock()
    a = store_factory(clock, ttl_seconds=1000, wait_seconds=0.2, lease_seconds=30)
    b = store_factory(clock, ttl_seconds=1000, wait_seconds=0.2, lease_seconds=30)
    return a, b, clock


@SHARED
def test_two_store_objects_on_one_file_see_each_others_commits(shared_pair):
    a, b, _ = shared_pair
    s = new_state()
    a.create(s)
    assert b.read(s.session_id) == s
    with b.turn(s.session_id) as t:
        t.state.turn = 1
        t.commit()
    assert a.read(s.session_id).turn == 1
    entered, release = threading.Event(), threading.Event()
    holder = _hold(a, s.session_id, entered, release, turn=2)
    with pytest.raises(ConversationBusy):  # exclusivity holds across store objects
        with b.turn(s.session_id, wait=0.2):
            pass
    assert b.stats()["turns_in_progress"] == 1
    release.set()
    holder.join(10)
    with b.turn(s.session_id, wait=1) as t:
        assert t.state.turn == 2
    assert a.stats()["sessions_held"] == b.stats()["sessions_held"] == 1


@SHARED
def test_a_crashed_workers_lease_lapses_and_its_late_commit_is_refused(shared_pair):
    a, b, clock = shared_pair
    s = new_state()
    a.create(s)
    crashed = a.turn(s.session_id)
    stuck = crashed.__enter__()  # took the conversation, then never came back (nor renewed it)
    with pytest.raises(ConversationBusy):
        with b.turn(s.session_id, wait=0.1):
            pass
    clock.advance(31)  # past the lease
    with b.turn(s.session_id, wait=0.1) as t:
        t.state.turn = 7
        t.commit()
    stuck.state.turn = 99
    stuck.state.messages.append({"role": "user", "text": "late", "turn": 99})
    with pytest.raises(ConversationBusy):
        stuck.commit()  # it would overwrite newer state, so nothing is written
    crashed.__exit__(None, None, None)  # releasing a lapsed lease does not disturb anyone
    assert a.read(s.session_id).turn == 7 and len(a.read(s.session_id).messages) == 1
    with b.turn(s.session_id, wait=0) as t:
        assert t.state.turn == 7


@SHARED
def test_the_cap_and_expiry_are_shared_by_every_store_on_the_file(shared_pair):
    a, b, clock = shared_pair
    a.max_sessions = b.max_sessions = 2
    first, second, third = new_state(), new_state(), new_state()
    a.create(first)
    clock.advance(1)
    b.create(second)
    clock.advance(1)
    a.create(third)
    with pytest.raises(SessionNotFound):
        b.read(first.session_id)
    assert b.stats()["sessions_held"] == 2
    clock.advance(2000)
    with pytest.raises(SessionNotFound):
        a.read(third.session_id)


_HOLDER = textwrap.dedent(
    """
    import sys, time
    from insurance_claims.store import PostgresStore, SqliteStore
    kind, where, session_id = sys.argv[1:4]
    store = SqliteStore(where) if kind == "sqlite" else PostgresStore(where)
    with store.turn(session_id) as t:
        print("holding", flush=True)
        time.sleep(1.0)
        t.state.turn += 1
        t.state.messages.append({"role": "user", "text": "from the other process", "turn": t.state.turn})
        t.commit()
    store.close()
    print("done", flush=True)
    """
)


@SHARED
def test_another_process_holding_a_conversation_is_waited_for(store_kind, request, tmp_path):
    where = str(tmp_path / "processes.db") if store_kind == "sqlite" else request.getfixturevalue("postgres_url")

    def open_it():
        return SqliteStore(where, wait_seconds=0.2) if store_kind == "sqlite" else PostgresStore(where, wait_seconds=0.2)

    store = open_it()
    s = new_state()
    store.create(s)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, store_kind, where, s.session_id],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "holding"
        with pytest.raises(ConversationBusy):
            with store.turn(s.session_id, wait=0.2):
                pass
        with store.turn(s.session_id, wait=10) as t:  # in once the other process commits
            assert t.state.turn == 1
            t.state.turn += 1
            t.commit()
        assert proc.wait(15) == 0 and proc.stdout.read().strip() == "done"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        store.close()
    reopened = open_it()
    try:
        back = reopened.read(s.session_id)  # and it is all still there after a restart
        assert back.turn == 2 and back.messages[-1]["text"] == "from the other process"
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("spec", "expected"), [
    ("", ("memory", None)),
    ("memory", ("memory", None)),
    ("MEMORY", ("memory", None)),
    ("sqlite:///data/sessions.db", ("sqlite", "data/sessions.db")),
    ("sqlite:////data/sessions.db", ("sqlite", "/data/sessions.db")),
    ("sqlite:///C:/data/sessions.db", ("sqlite", "C:/data/sessions.db")),
    ("/data/sessions.db", ("sqlite", "/data/sessions.db")),
    (r"C:\data\sessions.db", ("sqlite", r"C:\data\sessions.db")),
    ("postgresql://app:pw@db:5432/claims", ("postgres", "postgresql://app:pw@db:5432/claims")),
    ("postgres://db/claims?sslmode=require", ("postgres", "postgres://db/claims?sslmode=require")),
    (" PostgreSQL://db/claims ", ("postgres", "PostgreSQL://db/claims")),
])
def test_store_setting_is_parsed(spec, expected):
    assert parse_store_spec(spec) == expected


@pytest.mark.parametrize("spec", [
    "redis://localhost:6379",
    "redis://:hunter2-secret@cache:6379",
    "postgresql+psycopg://app:hunter2-secret@db/claims",
    "mysql://app:hunter2-secret@db/claims",
    "sqlite://x.db",
    ":memory:",
    "sqlite:///:memory:",
])
def test_store_setting_refuses_what_it_cannot_serve(spec):
    with pytest.raises(ValueError) as refused:
        parse_store_spec(spec)
    assert "hunter2-secret" not in str(refused.value)


def test_settings_never_show_the_store_url_or_the_data_key():
    from insurance_claims.config import load_settings

    cfg = dataclasses.replace(
        load_settings(), session_store="postgresql://app:hunter2-secret@db/claims", data_key="k" * 44
    )
    assert "hunter2-secret" not in repr(cfg) and "k" * 44 not in repr(cfg)


def test_open_store_follows_the_settings(tmp_path):
    from insurance_claims.config import load_settings

    cfg = load_settings()
    assert cfg.turn_lease_seconds == 20 and cfg.turn_max_seconds == 300
    assert cfg.audit_retention_seconds == 90 * 86400 and cfg.store_pool_size == 20 and cfg.data_key == ""
    memory = open_store(dataclasses.replace(cfg, session_store="memory", max_sessions=7, max_store_mb=3))
    assert isinstance(memory, MemoryStore) and memory.max_sessions == 7 and memory.max_bytes == 3 * 1024 * 1024
    sqlite = open_store(
        dataclasses.replace(
            cfg,
            session_store=str(tmp_path / "sub" / "s.db"),
            turn_lease_seconds=12,
            turn_max_seconds=90,
            audit_retention_seconds=3600,
        )
    )
    try:
        assert isinstance(sqlite, SqliteStore) and sqlite.lease_seconds == 12
        assert sqlite.turn_max_seconds == 90 and sqlite.audit_retention_seconds == 3600
        assert not sqlite.encrypted
        assert (tmp_path / "sub" / "s.db").exists()
    finally:
        sqlite.close()


@SHARED
def test_open_store_opens_the_shared_store_of_the_setting(store_kind, request, tmp_path):
    from insurance_claims.config import load_settings

    spec = f"sqlite:///{tmp_path / 's.db'}" if store_kind == "sqlite" else request.getfixturevalue("postgres_url")
    store = open_store(dataclasses.replace(load_settings(), session_store=spec, store_pool_size=3))
    try:
        assert isinstance(store, SqliteStore if store_kind == "sqlite" else PostgresStore)
        assert store.shared and store.kind == store_kind
        if store_kind == "postgres":
            assert store.pool_size == 3
        s = new_state()
        store.create(s)
        assert store.read(s.session_id) == s
    finally:
        store.close()
