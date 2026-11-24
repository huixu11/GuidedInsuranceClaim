"""What the API shows of delivery and health: effects queued with a turn's commit and their
delivery status on the conversation, the audit endpoint, /readyz, /api/stats, the dispatcher
the app's lifespan runs, and a shutdown that does not commit a turn cut off by its grace period.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from insurance_claims import server
from insurance_claims.dispatch import DemoEmail, DemoHandoff, Dispatcher
from insurance_claims.store import StoreUnavailable

from .test_serving import DEMO, Gate, envelope, snapshot

EMAIL_SCRIPT = [DEMO, "How do I submit the missing documents?", "That's all I needed, thanks.", "Yes, please send it."]
HUMAN = "I'd like to speak to a human representative, please."


def say(client: TestClient, sid: str, text: str, message_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text}
    if message_id:
        body["message_id"] = message_id
    r = client.post(f"/api/sessions/{sid}/messages", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def without_delivery(view: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(view))
    for entry in (out.get("email") or {}).get("outbox") or []:
        entry.pop("delivery", None)
    if isinstance(out.get("escalation"), dict):
        out["escalation"].pop("delivery", None)
    return out


def wait_for(condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Effects queued with the commit, and their delivery status
# ---------------------------------------------------------------------------
def test_the_summary_email_is_queued_with_its_turn_and_shown_until_sent(app_on, repo):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    for text in EMAIL_SCRIPT[:-1]:
        assert say(client, sid, text)["state"]["email"]["outbox"] == []
    assert rt.store.effects(sid) == []
    out = say(client, sid, EMAIL_SCRIPT[-1])
    assert "the summary is on its way" in out["reply"]  # queued, not claimed as sent
    (entry,) = out["state"]["email"]["outbox"]
    assert entry["delivery"] == {"status": "queued", "attempts": 0, "sent_at": None}
    address = repo.policyholder("P9").email
    assert entry["to"] != address and address not in json.dumps(out)

    (queued,) = rt.store.effects(sid)
    assert (queued["kind"], queued["key"], queued["status"]) == ("email", "email-0", "pending")
    read = client.get(f"/api/sessions/{sid}").json()["state"]
    assert read["email"]["outbox"][0]["delivery"] == {"status": "queued", "attempts": 0, "sent_at": None}

    Dispatcher(rt.store, repo, rt.settings, email=DemoEmail(), handoff=DemoHandoff()).run_once()
    delivered = client.get(f"/api/sessions/{sid}").json()["state"]["email"]["outbox"][0]["delivery"]
    assert delivered["status"] == "sent" and delivered["attempts"] == 1 and delivered["sent_at"] > 0
    # Only the delivery status changed; the next turn queues nothing new.
    assert without_delivery(client.get(f"/api/sessions/{sid}").json()["state"]) == without_delivery(out["state"])
    after = say(client, sid, "Thanks, bye.")
    assert "delivery" not in after["state"]["email"]["outbox"][0]  # not queued by this turn: read the conversation
    assert len(rt.store.effects(sid)) == 1


def test_a_handoff_is_queued_once_with_the_turn_that_made_it(app_on):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    out = say(client, sid, HUMAN)
    assert out["state"]["status"] == "escalated"
    assert out["state"]["escalation"]["delivery"] == {"status": "queued", "attempts": 0, "sent_at": None}
    say(client, sid, "Hello? Is anyone there?")
    effects = rt.store.effects(sid)
    assert [(e["kind"], e["key"]) for e in effects] == [("handoff", "handoff")]
    shown = client.get(f"/api/sessions/{sid}").json()["state"]["escalation"]["delivery"]
    assert shown["status"] == "queued"


def test_a_turn_that_fails_queues_nothing(app_on, monkeypatch):
    from insurance_claims.llm import LLMError

    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]

    def failing(state, text, backend, **kwargs):
        state.escalation = {"reason": "caller_requested", "turn": 1, "handoff_summary": "x"}
        raise LLMError("model_unavailable", "The model service is unavailable.", 503, retry_after=4)

    monkeypatch.setattr(server.agent, "handle_turn", failing)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": HUMAN})
    assert r.status_code == 503 and envelope(r) == "model_unavailable" and r.headers["Retry-After"] == "4"
    assert rt.store.effects(sid) == []


# ---------------------------------------------------------------------------
# The audit endpoint
# ---------------------------------------------------------------------------
def test_the_audit_endpoint_lists_events_and_effects_without_content(app_on, repo):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    for text in EMAIL_SCRIPT:
        say(client, sid, text)
    audit = client.get(f"/api/sessions/{sid}/audit")
    assert audit.status_code == 200
    body = audit.json()
    assert set(body) == {"session_id", "events", "effects"} and body["session_id"] == sid
    types = [e["type"] for e in body["events"]]
    assert types[0] == "conversation_started" and "identity_checked" in types
    (effect,) = body["effects"]
    assert set(effect) == {"id", "kind", "key", "status", "attempts", "created_at", "sent_at", "last_error"}
    assert (effect["kind"], effect["key"], effect["status"]) == ("email", "email-0", "pending")
    blob = json.dumps(body)
    assert repo.policyholder("P9").email not in blob and "Hello Margaret" not in blob and "4472" not in blob

    for unknown in ("0123456789abcdef0123456789abcdef", "not a valid id!"):
        r = client.get(f"/api/sessions/{unknown}/audit")
        assert r.status_code == 404 and envelope(r) == "session_not_found"


# ---------------------------------------------------------------------------
# /readyz and /api/stats
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("store_kind", ["memory", "sqlite"], indirect=True)
def test_readyz_says_whether_the_store_answers(app_on, monkeypatch):
    client, rt = app_on()
    assert client.get("/readyz").json() == {"status": "ready"}

    def down() -> None:
        raise StoreUnavailable("the store is down")

    monkeypatch.setattr(rt.store, "ping", down)
    r = client.get("/readyz")
    assert r.status_code == 503 and envelope(r) == "not_ready" and r.headers["Retry-After"] == "1"
    assert client.get("/healthz").json() == {"status": "ok"}  # liveness does not depend on the store

    release = threading.Event()
    monkeypatch.setattr(rt.store, "ping", lambda: release.wait(10))
    monkeypatch.setattr(server, "READY_TIMEOUT_SECONDS", 0.3)
    started = time.perf_counter()
    r = client.get("/readyz")
    assert r.status_code == 503 and envelope(r) == "not_ready"
    assert time.perf_counter() - started < 2.0  # a store that hangs does not hang the check
    release.set()

    monkeypatch.setattr(rt.store, "ping", lambda: None)
    assert client.get("/readyz").status_code == 200
    rt.closing = True
    r = client.get("/readyz")
    assert r.status_code == 503 and "shutting down" in r.json()["error"]["message"]


def test_stats_show_the_model_the_dispatcher_and_replays(app_on, repo):
    client, rt = app_on(dispatch=True, dispatch_poll_seconds=5)
    assert rt.dispatcher is not None and not rt.dispatcher.running  # the lifespan starts it
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    say(client, sid, DEMO, "stats-message-1")
    say(client, sid, DEMO, "stats-message-1")
    stats = client.get("/api/stats").json()
    assert stats["counters"]["replayed_messages"] == 1 and stats["counters"]["turns_answered"] == 1
    assert stats["model"]["state"] == "closed"  # the circuit breaker (the offline double makes no model call)
    d = stats["dispatcher"]
    assert d["enabled"] is True and d["running"] is False and d["email_transport"] == "demo"
    assert {"delivered", "retried", "dead", "store_errors"} <= set(d)
    assert sid not in json.dumps(stats)


# ---------------------------------------------------------------------------
# The lifespan: the dispatcher, and the shutdown
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("store_kind", ["memory", "sqlite"], indirect=True)
def test_the_app_runs_a_dispatcher_that_delivers_what_turns_queue(app_on, caplog):
    caplog.set_level("INFO", logger="sop.dispatch")
    _, rt = app_on(dispatch=True, dispatch_poll_seconds=0.05)
    with TestClient(server.app) as client:
        assert client.get("/api/stats").json()["dispatcher"]["running"] is True
        sid = client.post("/api/sessions", json={}).json()["session_id"]
        for text in EMAIL_SCRIPT:
            say(client, sid, text)
        handoff = client.post("/api/sessions", json={}).json()["session_id"]
        say(client, handoff, HUMAN)

        def delivered() -> bool:
            email_state = client.get(f"/api/sessions/{sid}").json()["state"]["email"]["outbox"][0]["delivery"]
            escalation = client.get(f"/api/sessions/{handoff}").json()["state"]["escalation"]["delivery"]
            return email_state["status"] == escalation["status"] == "sent"

        wait_for(delivered)
        assert client.get("/api/stats").json()["dispatcher"]["delivered"] == 2
    assert rt.dispatcher is not None and not rt.dispatcher.running and rt.closing
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name == "sop.dispatch")
    assert f"{sid[:8]}:email-0" in logged and sid not in logged


def test_a_turn_still_running_when_the_store_closes_is_not_committed(app_on, monkeypatch):
    client, rt = app_on(conversation_wait_seconds=5)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    before = snapshot(client, sid)
    gate = Gate(monkeypatch)
    with ThreadPoolExecutor(1) as pool:
        held = pool.submit(client.post, f"/api/sessions/{sid}/messages", json={"text": DEMO, "message_id": "late-turn-01"})
        gate.wait_entered(1)
        rt.closing = True  # what the lifespan sets once the grace period is over
        gate.release.set()
        r = held.result()
    assert r.status_code == 503 and envelope(r) == "server_busy" and r.headers["Retry-After"] == "1"
    rt.closing = False
    assert snapshot(client, sid) == before  # not committed, and the conversation was released
    retry = client.post(f"/api/sessions/{sid}/messages", json={"text": DEMO, "message_id": "late-turn-01"}).json()
    assert retry["replayed"] is False and retry["state"]["turn"] == 1


def _serve(grace: float) -> tuple[uvicorn.Server, threading.Thread, str]:
    """The app on a real uvicorn server (on a port the system picks), in a thread."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config = uvicorn.Config(server.app, lifespan="on", timeout_graceful_shutdown=grace, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: instance.started, timeout=10)
    return instance, thread, base


@pytest.mark.parametrize("store_kind", ["memory"], indirect=True)
@pytest.mark.parametrize("finishes_in_time", [True, False], ids=["within-grace", "cut-off"])
def test_a_shutdown_lets_turns_finish_within_the_grace_period(app_on, monkeypatch, finishes_in_time):
    client, rt = app_on()
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    gate = Gate(monkeypatch)
    instance, thread, base = _serve(grace=5.0 if finishes_in_time else 0.3)

    def send() -> int:
        try:
            return httpx.post(f"{base}/api/sessions/{sid}/messages", json={"text": DEMO}, timeout=15).status_code
        except httpx.HTTPError:
            return 0  # the connection was closed

    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(send)
        gate.wait_entered(1)
        instance.should_exit = True  # what SIGTERM does
        if finishes_in_time:
            time.sleep(0.3)
            assert not rt.closing  # still in the grace period
            # No longer accepting (refused; Windows retries a refused connection until the timeout).
            with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                httpx.get(f"{base}/healthz", timeout=1)
        else:
            wait_for(lambda: rt.closing, timeout=10)  # the grace period is over
        gate.release.set()
        status = pending.result()
    thread.join(15)
    assert not thread.is_alive()
    turn = rt.store.read(sid).turn
    if finishes_in_time:
        assert status == 200 and turn == 1
    else:
        assert status != 200 and turn == 0  # cut off: not committed
