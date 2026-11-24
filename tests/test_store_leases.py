"""Leases in the SQL stores: a turn holds its conversation by a lease that one background thread
per store object renews (about every lease/3) while the turn runs, for at most turn_max_seconds.
A process that dies stops renewing, so its conversations are free again within one lease; a
commit after the conversation was taken by another turn writes nothing."""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from insurance_claims.state import SessionState
from insurance_claims.store import ConversationBusy, PostgresStore, SqliteStore

PROJECT_DIR = Path(__file__).resolve().parent.parent
SQL = pytest.mark.parametrize("store_kind", ["sqlite", "postgres"], indirect=True)


def new_state() -> SessionState:
    state = SessionState(session_id=uuid.uuid4().hex)
    state.messages.append({"role": "assistant", "text": "Hello", "turn": 0})
    return state


def busy(store, session_id: str) -> bool:
    try:
        with store.turn(session_id, wait=0):
            return False
    except ConversationBusy:
        return True


@SQL
def test_renewing_keeps_a_turn_past_its_first_lease(store_factory):
    a = store_factory(lease_seconds=30)
    b = store_factory(lease_seconds=30, clock=a.test_clock)
    clock = a.test_clock
    s = new_state()
    a.create(s)
    with a.turn(s.session_id) as t:
        clock.advance(20)
        assert a.renew_leases() == 1
        clock.advance(20)  # 40 s: past the first lease, within the renewed one
        assert busy(b, s.session_id)
        assert b.stats()["turns_in_progress"] == 1
        t.state.turn = 1
        t.commit()
    assert a.renew_leases() == 0  # nothing held any more
    assert not busy(b, s.session_id) and b.read(s.session_id).turn == 1


@SQL
def test_renewal_stops_after_turn_max_and_the_late_commit_is_refused(store_factory, caplog):
    a = store_factory(lease_seconds=30, turn_max_seconds=60)
    b = store_factory(lease_seconds=30, clock=a.test_clock)
    clock = a.test_clock
    s = new_state()
    a.create(s)
    late = a.turn(s.session_id)
    stuck = late.__enter__()
    clock.advance(25)
    assert a.renew_leases() == 1
    clock.advance(25)
    assert a.renew_leases() == 1  # held until +80 s
    clock.advance(15)  # +65 s: past turn_max
    with caplog.at_level(logging.WARNING, logger="sop.store"):
        assert a.renew_leases() == 0
    assert any("no longer renewed" in r.getMessage() for r in caplog.records)
    assert busy(b, s.session_id)  # the last renewal still counts...
    clock.advance(16)  # ...until +81 s
    with b.turn(s.session_id, wait=0) as t:
        t.state.turn = 2
        t.commit()
    stuck.state.turn = 99
    with pytest.raises(ConversationBusy):
        stuck.commit()
    late.__exit__(None, None, None)
    assert b.read(s.session_id).turn == 2


@SQL
def test_a_lapsed_hold_that_nobody_took_still_commits(store_factory):
    store = store_factory(lease_seconds=30)
    s = new_state()
    store.create(s)
    with store.turn(s.session_id) as t:
        store.test_clock.advance(31)  # no renewal: lapsed, but no other turn wanted it
        t.state.turn = 1
        t.commit()
    assert store.read(s.session_id).turn == 1
    with store.turn(s.session_id, wait=0) as t:
        t.commit()


@SQL
def test_the_background_thread_renews_on_its_own(store_factory, store_kind):
    a = store_factory(real_time=True, lease_seconds=0.6, background=True)
    b = store_factory(real_time=True, lease_seconds=0.6)
    conversations = [new_state() for _ in range(4)]
    for s in conversations:
        a.create(s)
    entered = threading.Barrier(len(conversations) + 1)
    release = threading.Event()
    errors: list[BaseException] = []

    def hold(session_id: str) -> None:
        try:
            with a.turn(session_id, wait=5) as t:
                entered.wait(10)
                release.wait(10)
                t.state.turn = 1
                t.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    holders = [threading.Thread(target=hold, args=(s.session_id,)) for s in conversations]
    for h in holders:
        h.start()
    entered.wait(10)
    started = time.monotonic()
    while time.monotonic() - started < 2.0:  # more than three leases
        assert all(busy(b, s.session_id) for s in conversations)
        time.sleep(0.1)
    release.set()
    for h in holders:
        h.join(10)
    assert not errors, errors
    assert all(b.read(s.session_id).turn == 1 for s in conversations)
    # One renewal thread for the store object, however many turns it holds.
    renewers = [t for t in threading.enumerate() if t.name == f"sop-store-{store_kind}-renewals"]
    assert renewers == [a._bg_thread]
    a.close()
    assert not renewers[0].is_alive() and not [t for t in threading.enumerate() if t.name.endswith("-renewals")]


_CRASHING = textwrap.dedent(
    """
    import sys, time
    from insurance_claims.store import PostgresStore, SqliteStore
    kind, where, session_id = sys.argv[1:4]
    options = {"lease_seconds": 1.0, "wait_seconds": 1.0}
    store = SqliteStore(where, **options) if kind == "sqlite" else PostgresStore(where, **options)
    held = store.turn(session_id)  # kept: a context manager that is dropped releases its hold
    held.__enter__()
    print("holding", flush=True)
    time.sleep(60)
    """
)


@SQL
def test_a_crashed_process_frees_its_conversation_within_one_lease(store_factory, store_kind, request, tmp_path):
    where = str(tmp_path / "sessions.db") if store_kind == "sqlite" else request.getfixturevalue("postgres_url")
    store = (
        SqliteStore(where, lease_seconds=1.0, background=False)
        if store_kind == "sqlite"
        else PostgresStore(where, lease_seconds=1.0, background=False)
    )
    try:
        s = new_state()
        store.create(s)
        proc = subprocess.Popen(
            [sys.executable, "-c", _CRASHING, store_kind, where, s.session_id],
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "holding"
            started = time.monotonic()
            while time.monotonic() - started < 2.5:  # renewed: held well past its 1 s lease
                assert busy(store, s.session_id)
                time.sleep(0.2)
            proc.kill()
            proc.wait(10)
            killed = time.monotonic()
            with store.turn(s.session_id, wait=5) as t:
                freed_after = time.monotonic() - killed
                t.state.turn = 1
                t.commit()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
        print(f"{store_kind}: conversation free {freed_after:.2f} s after the holder was killed (lease 1 s)")
        assert freed_after < 1.6, freed_after  # one lease (1 s), plus the waiting turn's polling
        assert store.read(s.session_id).turn == 1
    finally:
        store.close()


def test_lease_settings_have_floors(monkeypatch):
    from insurance_claims.config import load_settings

    monkeypatch.setenv("SOP_TURN_LEASE_SECONDS", "1")
    monkeypatch.setenv("SOP_TURN_MAX_SECONDS", "2")
    monkeypatch.setenv("SOP_AUDIT_RETENTION_DAYS", "-3")
    monkeypatch.setenv("SOP_STORE_POOL_SIZE", "0")
    cfg = load_settings()
    # The hold is renewed for at least as long as the longest turn can run (see Settings).
    longest_turn = cfg.turn_budget_seconds + cfg.model_max_retries * cfg.model_timeout_seconds + 15
    assert cfg.turn_lease_seconds == 5 and cfg.turn_max_seconds == longest_turn > 5
    assert cfg.audit_retention_seconds == 0 and cfg.store_pool_size == 1
    monkeypatch.setenv("SOP_TURN_LEASE_SECONDS", "45")
    monkeypatch.setenv("SOP_TURN_MAX_SECONDS", "600")
    monkeypatch.setenv("SOP_AUDIT_RETENTION_DAYS", "7")
    cfg = load_settings()
    assert (cfg.turn_lease_seconds, cfg.turn_max_seconds, cfg.audit_retention_seconds) == (45, 600, 7 * 86400)


def test_a_store_refuses_a_lease_that_is_not_a_duration(tmp_path):
    for lease in (0, -1, float("inf")):
        with pytest.raises(ValueError):
            SqliteStore(tmp_path / "s.db", lease_seconds=lease)
    with pytest.raises(ValueError):
        SqliteStore(tmp_path / "s.db", turn_max_seconds=0)
