"""PostgreSQL only: what a database over the network adds. Outages (and starting before the
database is up), the connection pool, timeouts, instances starting together, and a password
that must never show. The servers come from conftest.py (embedded, or SOP_TEST_POSTGRES_URL);
the tests that stop and start a server need the embedded one."""

from __future__ import annotations

import logging
import threading
import time
import uuid

import pytest

from insurance_claims.state import SessionState
from insurance_claims.store import (
    PostgresStore,
    SessionNotFound,
    StoreError,
    StoreUnavailable,
    parse_store_spec,
    redact_url,
)

SECRET = "hunter2-Pg-Secret"


def new_state() -> SessionState:
    state = SessionState(session_id=uuid.uuid4().hex)
    state.messages.append({"role": "assistant", "text": "Hello", "turn": 0})
    return state


def with_password(url: str) -> str:
    """The URL with a password added (the embedded server trusts local connections, so any
    password is accepted)."""
    return url.replace("postgresql://postgres@", f"postgresql://postgres:{SECRET}@", 1)


def wait_until(check, timeout: float) -> float:
    """Seconds until check() stops raising StoreUnavailable."""
    started = time.monotonic()
    while True:
        try:
            check()
            return time.monotonic() - started
        except StoreUnavailable:
            if time.monotonic() - started > timeout:
                raise
            time.sleep(0.2)


@pytest.fixture()
def opened():
    stores: list[PostgresStore] = []

    def open_(url: str, **options) -> PostgresStore:
        options.setdefault("background", False)
        store = PostgresStore(url, **options)
        stores.append(store)
        return store

    yield open_
    for store in stores:
        store.close()


# ---------------------------------------------------------------------------
# Outages
# ---------------------------------------------------------------------------
def test_an_outage_is_reported_and_the_same_store_object_recovers(restartable_postgres, opened, caplog):
    server = restartable_postgres
    store = opened(with_password(server.url))
    s = new_state()
    store.create(s)
    with store.turn(s.session_id) as t:
        t.state.turn = 1
        t.commit()

    server.stop("fast")
    started = time.monotonic()
    with pytest.raises(StoreUnavailable) as down:
        store.read(s.session_id)
    noticed = time.monotonic() - started
    assert noticed < 7, noticed  # the pool timeout (5 s) at most
    started = time.monotonic()
    with pytest.raises(StoreUnavailable):
        store.create(new_state())
    with pytest.raises(StoreUnavailable):
        with store.turn(s.session_id):
            pass
    assert time.monotonic() - started < 0.5  # known to be down: the next requests fail at once

    server.start()
    recovered = wait_until(lambda: store.read(s.session_id), timeout=30)
    print(f"outage noticed after {noticed:.2f} s; reads work again {recovered:.2f} s after the restart")
    with store.turn(s.session_id) as t:  # the same store object: no restart needed
        assert t.state.turn == 1
        t.state.turn = 2
        t.commit()
    other = new_state()
    store.create(other)
    assert store.read(s.session_id).turn == 2 and store.read(other.session_id) == other
    assert SECRET not in str(down.value)
    assert not [r for r in caplog.records if SECRET in r.getMessage()]


def test_the_store_starts_before_the_database_and_sets_itself_up_later(restartable_postgres, opened, caplog):
    server = restartable_postgres
    server.stop("fast")
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="sop.store"):
        store = opened(with_password(server.url))  # does not raise: the process can start
    assert time.monotonic() - started < 10
    assert any("cannot be reached yet" in r.getMessage() for r in caplog.records)
    with pytest.raises(StoreUnavailable):
        store.ping()
    server.start()
    wait_until(store.ping, timeout=30)
    s = new_state()
    store.create(s)  # the schema was created on first use
    with store.turn(s.session_id) as t:
        t.commit()
    assert store.read(s.session_id) == s
    assert not [r for r in caplog.records if SECRET in r.getMessage()]


# ---------------------------------------------------------------------------
# The pool and timeouts
# ---------------------------------------------------------------------------
def test_no_connection_is_held_while_a_turn_runs(postgres_url, opened):
    store = opened(postgres_url, pool_size=1, wait_seconds=1)
    a, b = new_state(), new_state()
    store.create(a)
    store.create(b)
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with store.turn(a.session_id) as t:
            entered.set()
            release.wait(10)
            t.state.turn = 1
            t.commit()

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(10)
        started = time.monotonic()
        with store.turn(b.session_id) as t:  # the one pooled connection is free
            t.state.turn = 1
            t.commit()
        assert store.read(a.session_id).turn == 0
        assert time.monotonic() - started < 2
    finally:
        release.set()
        holder.join(10)
    assert store.read(a.session_id).turn == 1


def test_no_free_connection_in_time_is_store_unavailable(postgres_url, opened):
    store = opened(postgres_url, pool_size=1, pool_timeout=0.5)
    s = new_state()
    store.create(s)
    with store._get_pool().connection():  # someone else has the only connection
        started = time.monotonic()
        with pytest.raises(StoreUnavailable, match="no connection within"):
            store.read(s.session_id)
        assert time.monotonic() - started < 3
    assert store.read(s.session_id) == s


def test_a_lock_held_elsewhere_too_long_is_store_unavailable(postgres_url, opened):
    import psycopg

    store = opened(postgres_url)
    s = new_state()
    store.create(s)
    with psycopg.connect(postgres_url, autocommit=False) as other:  # a stuck transaction elsewhere
        other.execute("SELECT 1 FROM conversations WHERE id = %s FOR UPDATE", (s.session_id,))
        started = time.monotonic()
        with pytest.raises(StoreUnavailable):
            with store.turn(s.session_id, wait=1):
                pass
        waited = time.monotonic() - started
        other.rollback()
    assert waited < 6, waited  # the lock timeout (3 s), not forever
    with store.turn(s.session_id, wait=0) as t:
        t.commit()


def test_instances_starting_together_set_the_schema_up_once(postgres_url, opened):
    barrier = threading.Barrier(6)
    stores: list[PostgresStore] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def start() -> None:
        try:
            barrier.wait(10)
            store = opened(postgres_url)
            with lock:
                stores.append(store)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    created = []
    for store in stores:
        s = new_state()
        store.create(s)
        created.append(s)
    assert all(stores[0].read(s.session_id) == s for s in created)
    assert stores[0].stats()["sessions_held"] == 6


def test_the_count_stays_exact_when_many_instances_create_at_once(postgres_url, opened):
    stores = [opened(postgres_url, max_sessions=10_000) for _ in range(4)]
    errors: list[BaseException] = []

    def work(store) -> None:
        try:
            for _ in range(25):
                store.create(new_state())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(store,)) for store in stores for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    counted = stores[0]._session(lambda ex: ex.run(stores[0]._count_sql).scalar())
    actual = stores[0]._session(lambda ex: ex.run("SELECT COUNT(*) FROM conversations").scalar())
    assert counted == actual == 200


# ---------------------------------------------------------------------------
# The password
# ---------------------------------------------------------------------------
def test_the_password_never_shows(postgres_url, opened, caplog, unused_port):
    store = opened(with_password(postgres_url))
    s = new_state()
    store.create(s)
    shown = [repr(store), store.describe(), str(store.stats())]
    assert "postgres@127.0.0.1" in store.describe()

    with caplog.at_level(logging.DEBUG):
        unreachable = opened(f"postgresql://app:{SECRET}@127.0.0.1:{unused_port}/claims", pool_timeout=1)
        for operation in (unreachable.ping, lambda: unreachable.read("x"), lambda: unreachable.create(new_state())):
            with pytest.raises(StoreUnavailable) as failed:
                operation()
            shown.append(str(failed.value))
        shown += [repr(unreachable), unreachable.describe()]
    for bad in (
        f"postgresql://app:{SECRET}@[::1/claims",  # libpq quotes the whole URL in its error
        f"postgresql://app:{SECRET}@db/claims?nonsense=1",
        f"postgresql://app@db/claims?password={SECRET}&nonsense=1",
    ):
        with pytest.raises(ValueError) as refused:
            PostgresStore(bad)
        shown.append(str(refused.value))
    shown += [r.getMessage() for r in caplog.records] + [str(r.args) for r in caplog.records]
    leaked = [text for text in shown if SECRET in text]
    assert not leaked, leaked


def test_redaction_covers_the_url_forms():
    assert redact_url(f"postgresql://app:{SECRET}@db:5432/x") == "postgresql://app:***@db:5432/x"
    assert redact_url(f"postgres://app:{SECRET}@db/x?sslmode=require") == "postgres://app:***@db/x?sslmode=require"
    assert redact_url(f"postgresql://app@db/x?sslmode=require&password={SECRET}") == (
        "postgresql://app@db/x?sslmode=require&password=***"
    )
    assert redact_url("postgresql://app@db/x") == "postgresql://app@db/x"


@pytest.fixture()
def unused_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ---------------------------------------------------------------------------
# Configuration and serving
# ---------------------------------------------------------------------------
def test_several_workers_accept_a_postgres_store(monkeypatch):
    import uvicorn

    from insurance_claims import __main__ as cli

    started: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: started.update(options))
    monkeypatch.setenv("SOP_SESSION_STORE", f"postgresql://app:{SECRET}@db:5432/claims")
    cli.main(["--workers", "3"])
    assert started["workers"] == 3
    assert parse_store_spec(f"postgresql://app:{SECRET}@db/claims")[0] == "postgres"


def test_the_in_memory_store_refusal_names_both_shared_stores(monkeypatch, capsys):
    from insurance_claims import __main__ as cli

    monkeypatch.setenv("SOP_SESSION_STORE", "memory")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--workers", "2"])
    err = capsys.readouterr().err
    assert stopped.value.code == 2
    assert "SOP_SESSION_STORE=sqlite:///" in err and "postgresql://" in err


def test_a_database_without_the_tables_is_not_mistaken_for_an_empty_store(postgres_url, opened):
    store = opened(postgres_url)
    s = new_state()
    store.create(s)
    store._session(lambda ex: ex.run("DROP TABLE messages"))
    with pytest.raises(StoreError) as broken:
        store.read(s.session_id)
    assert not isinstance(broken.value, SessionNotFound)
