"""Idempotent messages: a message may carry a client-chosen `message_id`, sent again with every
retry of it. A retry of a message that was answered is answered from the transcript ("replayed":
true) and never runs the turn twice, on whichever instance it reaches; the same id with other
text is refused. Every store; the restart cases need one that outlives the process.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from insurance_claims import server
from insurance_claims.agent import Agent
from insurance_claims.llm import LLMError

from .test_serving import DEMO, Gate, envelope, snapshot

SQL = pytest.mark.parametrize("store_kind", ["sqlite", "postgres"], indirect=True)


def post(client: TestClient, sid: str, text: str, message_id: str | None = None):
    body: dict[str, Any] = {"text": text}
    if message_id is not None:
        body["message_id"] = message_id
    return client.post(f"/api/sessions/{sid}/messages", json=body)


def user_texts(client: TestClient, sid: str) -> list[str]:
    return [m["text"] for m in client.get(f"/api/sessions/{sid}").json()["messages"] if m["role"] == "user"]


def test_a_retried_message_is_answered_from_the_transcript(app_on):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    first = post(client, sid, DEMO, "a1b2c3d4e5f6")
    assert first.status_code == 200
    out = first.json()
    assert out["replayed"] is False and out["state"]["phase"] == "PROCESS_CASE"
    stored = client.get(f"/api/sessions/{sid}").json()
    assert stored["messages"][1] == {"role": "user", "text": DEMO, "turn": 1, "id": "a1b2c3d4e5f6"}

    again = post(client, sid, DEMO, "a1b2c3d4e5f6")
    assert again.status_code == 200
    replay = again.json()
    assert replay["replayed"] is True
    assert replay["reply"] == out["reply"] and replay["trace"] == out["trace"]
    assert replay["state"] == out["state"] and replay["state"]["turn"] == 1
    assert client.get(f"/api/sessions/{sid}").json() == stored  # nothing was written

    # Another message without an id, and one with a new id, run as usual.
    assert post(client, sid, "How do I submit the missing documents?").json()["state"]["turn"] == 2
    assert post(client, sid, "That's all I needed, thanks.", "f00dfeed-0001").json()["replayed"] is False
    # An older message's retry still gets its own answer, from its own turn.
    late = post(client, sid, DEMO, "a1b2c3d4e5f6").json()
    assert late["replayed"] is True and late["reply"] == out["reply"] and late["trace"]["turn"] == 1
    assert late["state"]["turn"] == 3  # the conversation as it is now
    assert user_texts(client, sid) == [DEMO, "How do I submit the missing documents?", "That's all I needed, thanks."]
    counters = client.get("/api/stats").json()["counters"]
    assert counters["replayed_messages"] == 2 and counters["turns_answered"] == 3


def test_two_attempts_of_one_message_at_once_run_it_once(app_on, monkeypatch):
    """The retry arrives while the first attempt is still running: it waits for the conversation
    like any message, then finds the answer in the transcript."""
    client, rt = app_on(conversation_wait_seconds=10)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    gate = Gate(monkeypatch)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(post, client, sid, DEMO, "same-message-01")
        gate.wait_entered(1)
        second = pool.submit(post, client, sid, DEMO, "same-message-01")
        time.sleep(0.3)  # the second attempt is now waiting for the conversation
        assert not second.done()
        gate.release.set()
        a, b = first.result(), second.result()
    assert a.status_code == b.status_code == 200
    assert (a.json()["replayed"], b.json()["replayed"]) == (False, True)
    assert a.json()["reply"] == b.json()["reply"]
    assert gate.order == [sid]  # the engine ran once
    assert user_texts(client, sid) == [DEMO]
    assert client.get(f"/api/sessions/{sid}").json()["state"]["turn"] == 1


def test_a_retry_that_waits_too_long_gets_409_then_its_answer(app_on, monkeypatch):
    client, rt = app_on(conversation_wait_seconds=0.3)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    gate = Gate(monkeypatch)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(post, client, sid, DEMO, "slow-message-1")
        gate.wait_entered(1)
        busy = post(client, sid, DEMO, "slow-message-1")
        assert busy.status_code == 409 and envelope(busy) == "conversation_busy"
        assert busy.headers["Retry-After"] == "1"
        gate.release.set()
        assert first.result().json()["replayed"] is False
    again = post(client, sid, DEMO, "slow-message-1")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert user_texts(client, sid) == [DEMO]


def test_a_first_attempt_that_failed_before_its_commit_runs_again(app_on, monkeypatch):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    before = snapshot(client, sid)
    real = server.agent.handle_turn
    calls = {"n": 0}

    def flaky(state, text, backend, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            state.messages.append({"role": "user", "text": text, "turn": 1, "id": kwargs.get("message_id")})
            raise LLMError("model_overloaded", "The model service is overloaded.", 503, retry_after=7)
        return real(state, text, backend, **kwargs)

    monkeypatch.setattr(server.agent, "handle_turn", flaky)
    failed = post(client, sid, DEMO, "retry-me-0001")
    assert failed.status_code == 503 and envelope(failed) == "model_overloaded"
    assert failed.headers["Retry-After"] == "7"  # the model error's own Retry-After
    assert snapshot(client, sid) == before  # nothing was committed, the id included
    retry = post(client, sid, DEMO, "retry-me-0001")
    assert retry.status_code == 200 and retry.json()["replayed"] is False
    assert retry.json()["state"]["phase"] == "PROCESS_CASE" and calls["n"] == 2
    counters = client.get("/api/stats").json()["counters"]
    assert counters["model_errors"] == 1 and counters["replayed_messages"] == 0


def test_the_same_id_with_other_text_is_refused_and_changes_nothing(app_on):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    assert post(client, sid, "What is RL?", "reused-id-001").status_code == 200
    before = snapshot(client, sid)
    r = post(client, sid, "My name is Margaret Chen.", "reused-id-001")
    assert r.status_code == 409 and envelope(r) == "message_id_reused"
    assert snapshot(client, sid) == before
    # Ids are per conversation: another conversation may use the same one.
    other = client.post("/api/sessions", json={}).json()["session_id"]
    assert post(client, other, "My name is Margaret Chen.", "reused-id-001").json()["replayed"] is False


def test_the_last_message_a_conversation_may_hold_is_replayed_not_refused(app_on, monkeypatch):
    client, rt = app_on()
    monkeypatch.setattr(server, "agent", Agent(server.repo, dataclasses.replace(server.agent.settings,
                                                                              max_turns_per_conversation=2)))
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    assert post(client, sid, "What is RL?").status_code == 200
    last = post(client, sid, "What is RL?", "the-last-one-1")
    assert last.status_code == 200 and last.json()["state"]["status"] == "escalated"
    again = post(client, sid, "What is RL?", "the-last-one-1")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["reply"] == last.json()["reply"]
    closed = post(client, sid, "Hello?", "after-the-cap-1")
    assert closed.status_code == 409 and envelope(closed) == "conversation_closed"


def test_a_message_to_an_escalated_conversation_is_replayed_too(app_on):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    handed = post(client, sid, "I'd like to speak to a human representative, please.", "ask-human-0001").json()
    assert handed["state"]["status"] == "escalated"
    after = post(client, sid, "Hello? Anyone there?", "after-handoff-1").json()
    assert after["replayed"] is False and after["state"]["turn"] == 2
    again = post(client, sid, "Hello? Anyone there?", "after-handoff-1").json()
    assert again["replayed"] is True and again["reply"] == after["reply"] and again["state"]["turn"] == 2
    assert post(client, sid, "I'd like to speak to a human representative, please.", "ask-human-0001").json()["replayed"]
    assert len(user_texts(client, sid)) == 2


@SQL
def test_a_retry_after_a_restart_is_answered_by_the_new_process(app_on, store_factory):
    """The first instance answered and went away; the retry reaches a new one on the same store."""
    client, first_rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    out = post(client, sid, DEMO, "survives-restart").json()
    first_rt.store.close()

    client, second_rt = app_on(store=store_factory(real_time=True))  # same file or database
    assert second_rt is not first_rt
    again = post(client, sid, DEMO, "survives-restart")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["reply"] == out["reply"] and again.json()["trace"] == out["trace"]
    assert user_texts(client, sid) == [DEMO]
    assert client.get("/api/stats").json()["counters"]["replayed_messages"] == 1


@pytest.mark.parametrize("store_kind", ["memory"], indirect=True)
@pytest.mark.parametrize(
    "bad", ["short", "x" * 65, "has space1", "semi;colon1", "ümlaut-0001", "", 12345678, ["a1b2c3d4e5"]]
)
def test_a_malformed_message_id_is_refused(app_on, bad):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    before = snapshot(client, sid)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "What is RL?", "message_id": bad})
    assert r.status_code == 400 and envelope(r) == "bad_request"
    assert snapshot(client, sid) == before
    ok = client.post(f"/api/sessions/{sid}/messages", json={"text": "What is RL?", "message_id": None})
    assert ok.status_code == 200 and ok.json()["replayed"] is False


@pytest.mark.parametrize("store_kind", ["memory"], indirect=True)
def test_many_retries_of_many_messages_at_once(app_on):
    """Every message sent three times at once: each turn runs once, every attempt gets the answer."""
    client, rt = app_on(conversation_wait_seconds=10)
    sids = [client.post("/api/sessions", json={}).json()["session_id"] for _ in range(8)]
    script = ["What is RL?", "My name is Margaret Chen.", "My date of birth is 1985-03-15."]
    lock = threading.Lock()
    replies: dict[tuple[str, int], set[str]] = {}

    def attempt(sid: str, n: int) -> None:
        r = post(client, sid, script[n], f"{sid[:8]}-msg-{n}")
        assert r.status_code == 200, r.text
        with lock:
            replies.setdefault((sid, n), set()).add(r.json()["reply"])

    with ThreadPoolExecutor(24) as pool:
        for n in range(len(script)):  # one message at a time per conversation, its attempts at once
            for f in [pool.submit(attempt, sid, n) for sid in sids for _ in range(3)]:
                f.result()
    for sid in sids:
        assert user_texts(client, sid) == script
    assert all(len(seen) == 1 for seen in replies.values())  # every attempt got the same answer
    counters = client.get("/api/stats").json()["counters"]
    assert counters["turns_answered"] == len(sids) * len(script)
    assert counters["replayed_messages"] == 2 * len(sids) * len(script)
