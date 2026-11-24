"""The outbox: effects queued atomically with a turn's commit, claimed by one delivery worker at a
time, and finished as sent, retried later, or dead. Every store; the cross-object cases need a
store shared between processes."""

from __future__ import annotations

import threading
import uuid

import pytest

from insurance_claims.state import SessionState
from insurance_claims.store import ConversationBusy, StoreError

SQL = pytest.mark.parametrize("store_kind", ["sqlite", "postgres"], indirect=True)


def new_state() -> SessionState:
    state = SessionState(session_id=uuid.uuid4().hex)
    state.messages.append({"role": "assistant", "text": "Hello", "turn": 0})
    return state


def effect(key: str, kind: str = "email", **payload) -> dict:
    return {"kind": kind, "key": key, "payload": payload or {"key": key}}


def queue(store, session_id: str, *effects: dict) -> None:
    with store.turn(session_id) as t:
        t.state.turn += 1
        t.commit(effects=list(effects))


def test_effects_are_queued_with_the_commit(store_factory):
    store = store_factory()
    s = new_state()
    store.create(s)
    with store.turn(s.session_id) as t:
        t.state.turn = 1
        t.commit(effects=[effect("summary-email", to="m***@email.com"), effect("handoff", kind="handoff")])
    listed = store.effects(s.session_id)
    assert [(e["kind"], e["key"], e["status"], e["attempts"]) for e in listed] == [
        ("email", "summary-email", "pending", 0),
        ("handoff", "handoff", "pending", 0),
    ]
    first = listed[0]
    assert set(first) == {"id", "kind", "key", "status", "attempts", "created_at", "sent_at", "last_error"}
    assert isinstance(first["created_at"], float) and first["sent_at"] is None and first["last_error"] is None
    assert listed[0]["id"] < listed[1]["id"]
    assert store.read(s.session_id).turn == 1
    assert store.stats()["effects"] == {"pending": 2, "sent": 0, "dead": 0}


def test_a_turn_that_fails_queues_nothing(store_factory):
    store = store_factory()
    s = new_state()
    store.create(s)
    with pytest.raises(RuntimeError):
        with store.turn(s.session_id) as t:
            t.state.turn = 1
            raise RuntimeError("the model call failed before the commit")
    # A malformed effect is refused before anything is written, the state included.
    for bad in (
        [{"kind": "email", "key": "k"}],  # no payload
        [{"kind": "email", "key": "", "payload": {}}],
        [{"kind": "email", "key": "k", "payload": {"x": object()}}],
        [{"kind": "email", "key": "k", "payload": "not a dict"}],
        [{"kind": "e", "key": "k", "payload": {}, "extra": 1}],
        [effect("fine"), {"kind": "email", "key": "x" * 201, "payload": {}}],
    ):
        with pytest.raises(ValueError):
            with store.turn(s.session_id) as t:
                t.state.turn = 2
                t.commit(effects=bad)
    assert store.effects(s.session_id) == [] and store.read(s.session_id).turn == 0
    # A commit that fails in the store (here: the append-only rule) queues nothing either.
    with pytest.raises(StoreError):
        with store.turn(s.session_id) as t:
            t.state.messages.clear()
            t.commit(effects=[effect("not-queued")])
    assert store.effects(s.session_id) == []
    queue(store, s.session_id, effect("queued"))  # and the conversation was released each time
    assert [e["key"] for e in store.effects(s.session_id)] == ["queued"]


@SQL
def test_a_commit_refused_after_a_lost_lease_queues_nothing(store_factory):
    a = store_factory(lease_seconds=30)
    b = store_factory(lease_seconds=30, clock=a.test_clock)
    s = new_state()
    a.create(s)
    late = a.turn(s.session_id)
    stuck = late.__enter__()
    a.test_clock.advance(31)
    queue(b, s.session_id, effect("from-b"))
    with pytest.raises(ConversationBusy):
        stuck.commit(effects=[effect("from-the-late-turn")])
    late.__exit__(None, None, None)
    assert [e["key"] for e in a.effects(s.session_id)] == ["from-b"]


def test_a_key_is_queued_once_per_conversation(store_factory):
    store = store_factory()
    s, other = new_state(), new_state()
    store.create(s)
    store.create(other)
    queue(store, s.session_id, effect("summary", n=1), effect("summary", n=2), effect("handoff"))
    queue(store, s.session_id, effect("summary", n=3))  # a retried turn queues it again: ignored
    queue(store, other.session_id, effect("summary", n=4))  # another conversation's own
    assert [e["key"] for e in store.effects(s.session_id)] == ["summary", "handoff"]
    claimed = {(c["session_id"], c["key"]): c["payload"] for c in store.claim_effects(limit=10)}
    assert claimed == {
        (s.session_id, "summary"): {"n": 1},  # the first one queued
        (s.session_id, "handoff"): {"key": "handoff"},
        (other.session_id, "summary"): {"n": 4},
    }


def test_claims_hand_out_each_due_effect_once(store_factory):
    store = store_factory()
    s = new_state()
    store.create(s)
    queue(store, s.session_id, *(effect(f"e{i}", i=i) for i in range(5)))
    first = store.claim_effects(limit=3, lease_seconds=60)
    assert [c["key"] for c in first] == ["e0", "e1", "e2"]  # oldest first
    assert {c["token"] for c in first} == {first[0]["token"]} and all(c["attempts"] == 1 for c in first)
    assert first[0]["payload"] == {"i": 0} and first[0]["session_id"] == s.session_id and first[0]["kind"] == "email"
    second = store.claim_effects(limit=3, lease_seconds=60)
    assert [c["key"] for c in second] == ["e3", "e4"] and second[0]["token"] != first[0]["token"]
    assert store.claim_effects() == []
    assert [e["attempts"] for e in store.effects(s.session_id)] == [1] * 5


def test_finishing_an_effect(store_factory):
    store = store_factory()
    clock = store.test_clock
    s = new_state()
    store.create(s)
    queue(store, s.session_id, effect("ok"), effect("retry"), effect("dead"), effect("lapsed"))
    claims = {c["key"]: c for c in store.claim_effects(limit=10, lease_seconds=30)}

    def status(key: str) -> dict:
        return next(e for e in store.effects(s.session_id) if e["key"] == key)

    # Sent: once, and finishing it again with the same claim changes nothing.
    assert store.finish_effect(claims["ok"]["id"], claims["ok"]["token"], ok=True)
    sent = status("ok")
    assert sent["status"] == "sent" and sent["sent_at"] is not None
    assert store.finish_effect(claims["ok"]["id"], claims["ok"]["token"], ok=True)
    assert status("ok") == sent
    # Another caller's token, or an id that does not exist: refused, nothing written.
    assert not store.finish_effect(claims["retry"]["id"], "not-the-token", ok=True)
    assert not store.finish_effect(10**9, claims["retry"]["token"], ok=True)
    assert status("retry")["status"] == "pending"
    # Retry: due again after retry_in, with the error kept (cut to 500 characters).
    assert store.finish_effect(claims["retry"]["id"], claims["retry"]["token"], ok=False, error="x" * 900, retry_in=60)
    retried = status("retry")
    assert retried["status"] == "pending" and retried["last_error"] == "x" * 500
    clock.advance(31)  # the claims have lapsed, but the retry is not due yet
    assert sorted(c["key"] for c in store.claim_effects(limit=10, lease_seconds=600)) == ["dead", "lapsed"]
    clock.advance(30)
    (again,) = store.claim_effects(limit=10)
    assert again["key"] == "retry" and again["attempts"] == 2
    # Dead: not ok and no retry.
    assert not store.finish_effect(claims["dead"]["id"], claims["dead"]["token"], ok=False, error="bad address")
    # (that claim lapsed and was taken again above, so its first token no longer counts)
    assert status("dead")["status"] == "pending"


def test_a_lapsed_claim_is_due_again_and_its_old_token_is_refused(store_factory):
    store = store_factory(ttl_seconds=100_000)  # the memory store's effects go with their conversation
    clock = store.test_clock
    s = new_state()
    store.create(s)
    queue(store, s.session_id, effect("e"))
    (first,) = store.claim_effects(lease_seconds=10)
    assert store.claim_effects(lease_seconds=10) == []  # claimed: not due
    clock.advance(11)  # the worker that claimed it died
    (second,) = store.claim_effects(lease_seconds=10)
    assert second["id"] == first["id"] and second["attempts"] == 2 and second["token"] != first["token"]
    assert not store.finish_effect(first["id"], first["token"], ok=True)  # too late: nothing written
    assert store.effects(s.session_id)[0]["status"] == "pending"
    assert store.finish_effect(second["id"], second["token"], ok=False, error="mailbox full")
    dead = store.effects(s.session_id)[0]
    assert dead["status"] == "dead" and dead["last_error"] == "mailbox full" and dead["attempts"] == 2
    clock.advance(1000)
    assert store.claim_effects() == []  # dead stays dead
    assert store.stats()["effects"] == {"pending": 0, "sent": 0, "dead": 1}


def test_claim_and_finish_arguments_are_checked(store_factory):
    store = store_factory()
    for limit, lease in ((0, 30), (1, 0), (1, -1), (1, float("nan"))):
        with pytest.raises(ValueError):
            store.claim_effects(limit=limit, lease_seconds=lease)
    with pytest.raises(ValueError):
        store.finish_effect(1, "t", ok=False, retry_in=-1)


def _claim_everything(stores: list, claimers: int) -> list[int]:
    """Claim from several threads at once until nothing is left; every id claimed."""
    claimed: list[int] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    start = threading.Barrier(claimers)

    def work(store) -> None:
        try:
            start.wait(10)
            empty = 0
            while empty < 3:
                got = store.claim_effects(limit=7, lease_seconds=600)
                empty = 0 if got else empty + 1
                with lock:
                    claimed.extend(c["id"] for c in got)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(stores[i % len(stores)],)) for i in range(claimers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    return claimed


def test_concurrent_claimers_never_get_the_same_effect(store_factory):
    store = store_factory()
    expected = []
    for _ in range(3):
        s = new_state()
        store.create(s)
        queue(store, s.session_id, *(effect(f"e{i}") for i in range(50)))
        expected += [e["id"] for e in store.effects(s.session_id)]
    claimed = _claim_everything([store], claimers=8)
    assert len(claimed) == len(set(claimed)) == 150 and set(claimed) == set(expected)


@SQL
def test_claimers_in_two_store_objects_never_get_the_same_effect(store_factory):
    a = store_factory()
    b = store_factory(clock=a.test_clock)
    expected = []
    for i in range(4):
        s = new_state()
        (a if i % 2 else b).create(s)
        queue(a, s.session_id, *(effect(f"e{j}") for j in range(50)))
        expected += [e["id"] for e in b.effects(s.session_id)]
    claimed = _claim_everything([a, b], claimers=8)
    assert len(claimed) == len(set(claimed)) == 200 and set(claimed) == set(expected)
