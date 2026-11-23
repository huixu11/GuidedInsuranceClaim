"""Serving under load: turn slots (503), one turn per conversation (409), body size (413), the
per-client rate limit (429), the message cap per conversation, /api/stats and the turn log, and
many conversations at once on one in-process app with either session store.

Every refusal must use the error envelope and leave the conversation exactly as it was.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from insurance_claims import server
from insurance_claims.agent import Agent, ConversationClosed
from insurance_claims.mock_llm import MockBackend

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEMO = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)


@pytest.fixture()
def serve(monkeypatch, tmp_path):
    """serve(store="memory" | "sqlite", **settings) installs a fresh Runtime (store, turn slots,
    rate limiter, counters) built from the test settings with those changes."""
    made: list[server.Runtime] = []

    def make(store: str = "memory", **changes: Any) -> server.Runtime:
        if store == "sqlite":
            changes["session_store"] = str(tmp_path / f"sessions-{len(made)}.db")
        rt = server.Runtime(dataclasses.replace(server.settings, **changes))
        made.append(rt)
        monkeypatch.setattr(server, "runtime", rt)
        return rt

    yield make
    for rt in made:
        rt.store.close()


class Gate:
    """Holds turns open inside the engine (after the conversation and a slot are taken) until
    released, so a test can fill the server deterministically."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()
        self.order: list[str] = []  # conversations, in the order their turns reached the engine
        real = server.agent.handle_turn

        def held_turn(state, text, backend):
            self.order.append(state.session_id)
            self.entered.release()
            assert self.release.wait(20), "the test never released the turn"
            return real(state, text, backend)

        monkeypatch.setattr(server.agent, "handle_turn", held_turn)

    def wait_entered(self, n: int) -> None:
        for _ in range(n):
            assert self.entered.acquire(timeout=10), "a turn never reached the engine"


def envelope(r) -> str:
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body
    assert body["error"]["message"]
    return body["error"]["code"]


def snapshot(client: TestClient, sid: str) -> dict[str, Any]:
    data = client.get(f"/api/sessions/{sid}").json()
    return {"messages": data["messages"], "state": data["state"], "traces": len(data["traces"])}


# ---------------------------------------------------------------------------
# 503: every turn slot busy
# ---------------------------------------------------------------------------
def test_a_full_server_refuses_turns_but_answers_everything_else(serve, monkeypatch, caplog):
    serve(max_concurrent_turns=2, turn_slot_wait_seconds=0.3)
    caplog.set_level(logging.INFO, logger="sop.turn")
    gate = Gate(monkeypatch)
    with TestClient(server.app) as client:
        sids = [client.post("/api/sessions", json={}).json()["session_id"] for _ in range(3)]
        idle_before = snapshot(client, sids[2])
        with ThreadPoolExecutor(2) as pool:
            held = [pool.submit(client.post, f"/api/sessions/{sid}/messages", json={"text": DEMO})
                    for sid in sids[:2]]
            gate.wait_entered(2)  # both slots are now taken by turns that do not finish

            for path in ("/healthz", "/api/config", "/api/stats", "/api/fixtures", "/",
                         "/static/app.js", "/static/styles.css", f"/api/sessions/{sids[2]}"):
                started = time.perf_counter()
                r = client.get(path)
                assert r.status_code == 200, path
                assert time.perf_counter() - started < 1.0, f"{path} waited behind the turns"
            assert client.post("/api/sessions", json={}).status_code == 200  # creation needs no slot
            slots = client.get("/api/stats").json()["turn_slots"]
            assert slots == {"in_use": 2, "max": 2, "waiting": 0}

            started = time.perf_counter()
            r = client.post(f"/api/sessions/{sids[2]}/messages", json={"text": DEMO})
            waited = time.perf_counter() - started
            assert r.status_code == 503 and envelope(r) == "server_busy"
            assert int(r.headers["Retry-After"]) >= 1
            assert 0.25 <= waited < 2.0, waited  # the short wait for a slot, then the refusal

            gate.release.set()
            assert [f.result().status_code for f in held] == [200, 200]
        assert snapshot(client, sids[2]) == idle_before  # the refused turn changed nothing
        counters = client.get("/api/stats").json()["counters"]
        assert counters["refused_busy_503"] == 1 and counters["turns_answered"] == 2
        lines = [r.getMessage() for r in caplog.records if r.name == "sop.turn"]
        assert sum(f"turn {sids[2][:8]} phase=?->? status=503 server_busy" in line for line in lines) == 1


def test_a_turn_waits_briefly_for_a_slot_first_come_first_served(serve, monkeypatch):
    serve(max_concurrent_turns=1, turn_slot_wait_seconds=10)
    gate = Gate(monkeypatch)
    with TestClient(server.app) as client:
        sids = [client.post("/api/sessions", json={}).json()["session_id"] for _ in range(3)]

        def send(sid: str) -> int:
            return client.post(f"/api/sessions/{sid}/messages", json={"text": "What is RL?"}).status_code

        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(send, sids[0])
            gate.wait_entered(1)
            second = pool.submit(send, sids[1])
            _wait_for(lambda: client.get("/api/stats").json()["turn_slots"]["waiting"] == 1)
            third = pool.submit(send, sids[2])
            _wait_for(lambda: client.get("/api/stats").json()["turn_slots"]["waiting"] == 2)
            gate.release.set()
            assert [f.result() for f in (first, second, third)] == [200, 200, 200]
        assert gate.order == sids  # the slot went to the longest waiter each time
        assert client.get("/api/stats").json()["turn_slots"] == {"in_use": 0, "max": 1, "waiting": 0}


def _wait_for(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# 409: the same conversation is mid-turn
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("store", ["memory", "sqlite"])
def test_a_second_message_while_one_is_answered_waits_then_gets_409(serve, monkeypatch, store):
    serve(store, conversation_wait_seconds=0.3)
    gate = Gate(monkeypatch)
    with TestClient(server.app) as client:
        sid = client.post("/api/sessions", json={}).json()["session_id"]
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(client.post, f"/api/sessions/{sid}/messages", json={"text": "My name is Margaret Chen."})
            gate.wait_entered(1)
            started = time.perf_counter()
            r = client.post(f"/api/sessions/{sid}/messages", json={"text": "What is RL?"})
            assert r.status_code == 409 and envelope(r) == "conversation_busy"
            assert r.headers["Retry-After"] == "1"
            assert 0.25 <= time.perf_counter() - started < 3.0
            gate.release.set()
            assert first.result().status_code == 200
        after = snapshot(client, sid)
        assert after["state"]["turn"] == 1 and after["traces"] == 1
        assert [m["text"] for m in after["messages"] if m["role"] == "user"] == ["My name is Margaret Chen."]
        assert client.get("/api/stats").json()["counters"]["conflicts_409"] == 1


# ---------------------------------------------------------------------------
# 413: bodies over 16 KB
# ---------------------------------------------------------------------------
def test_bodies_over_16_kb_are_refused_before_they_are_parsed(serve):
    serve()
    client = TestClient(server.app)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    url = f"/api/sessions/{sid}/messages"
    before = snapshot(client, sid)
    json_headers = {"Content-Type": "application/json"}

    # Declared length over the limit: refused at once.
    r = client.post(url, content=json.dumps({"text": "a" * 20_000}).encode(), headers=json_headers)
    assert r.status_code == 413 and envelope(r) == "payload_too_large"

    # No declared length (chunked): counted as it arrives.
    def chunks():
        yield b'{"text": "'
        for _ in range(20):
            yield b"a" * 1000
        yield b'"}'

    r = client.post(url, content=chunks(), headers=json_headers)
    assert r.status_code == 413 and envelope(r) == "payload_too_large"
    r = client.post("/api/sessions", content=b" " * 17_000, headers=json_headers)
    assert r.status_code == 413 and envelope(r) == "payload_too_large"
    assert snapshot(client, sid) == before
    assert client.get("/api/stats").json()["counters"]["too_large_413"] == 3

    # The largest message allowed fits: 4,000 characters of four-byte UTF-8 is under 16 KB.
    text = "\U0001F642" * 4000
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    assert len(body) <= server.MAX_BODY_BYTES
    r = client.post(url, content=body, headers=json_headers)
    assert r.status_code == 200 and r.json()["state"]["turn"] == 1


# ---------------------------------------------------------------------------
# 429: optional per-client rate limit
# ---------------------------------------------------------------------------
def test_the_rate_limit_is_off_by_default(serve):
    rt = serve()
    assert rt.limiter is None
    client = TestClient(server.app)
    assert all(client.post("/api/sessions", json={}).status_code == 200 for _ in range(30))


def test_the_rate_limit_refuses_with_retry_after_and_changes_nothing(serve):
    serve(rate_limit_per_minute=4)  # 4 messages a minute; 1 new conversation a minute
    client = TestClient(server.app)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    r = client.post("/api/sessions", json={})
    assert r.status_code == 429 and envelope(r) == "rate_limited"
    assert 1 <= int(r.headers["Retry-After"]) <= 60

    url = f"/api/sessions/{sid}/messages"
    for _ in range(4):
        assert client.post(url, json={"text": "What is RL?"}).status_code == 200
    before = snapshot(client, sid)
    r = client.post(url, json={"text": "What is RL?"}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 429 and envelope(r) == "rate_limited"  # the header is not trusted by default
    assert 1 <= int(r.headers["Retry-After"]) <= 15
    assert snapshot(client, sid) == before
    assert client.get("/api/stats").json()["counters"]["rate_limited_429"] == 2


def test_behind_a_trusted_proxy_the_client_is_the_last_forwarded_hop(serve):
    serve(rate_limit_per_minute=4, trust_proxy=True)
    client = TestClient(server.app)

    def create(xff: str) -> int:
        return client.post("/api/sessions", json={}, headers={"X-Forwarded-For": xff}).status_code

    assert create("198.51.100.1, 203.0.113.1") == 200
    assert create("198.51.100.2, 203.0.113.2") == 200  # another client
    assert create("10.9.9.9, 203.0.113.1") == 429  # the first hop is whatever the client sent
    assert create("203.0.113.1") == 429


def test_rate_limiter_buckets_refill_and_stay_bounded():
    limiter = server.RateLimiter(60)  # one message a second; 15 new conversations a minute
    now = 1000.0
    assert all(limiter.take("a", "turn", now) == 0.0 for _ in range(60))
    assert limiter.take("a", "turn", now) == pytest.approx(1.0)
    assert limiter.take("a", "turn", now + 1.0) == 0.0
    assert limiter.take("b", "turn", now) == 0.0  # buckets are per client
    for i in range(limiter.MAX_CLIENTS + 50):
        limiter.take(f"client-{i}", "create", now)
    assert len(limiter._buckets) <= limiter.MAX_CLIENTS


# ---------------------------------------------------------------------------
# Message cap per conversation
# ---------------------------------------------------------------------------
def test_the_last_allowed_message_is_answered_by_a_handoff(repo, settings, backend):
    agent = Agent(repo, dataclasses.replace(settings, max_turns_per_conversation=3))
    state = agent.new_session()
    for text in ("What is RL?", "My name is Margaret Chen."):
        agent.handle_turn(state, text, backend)
    assert state.status == "active" and state.turn == 2
    replies_before = backend.replies_requested
    reply, trace = agent.handle_turn(state, "And my date of birth is 1985-03-15.", backend)
    assert state.turn == 3 and state.status == "escalated"
    assert state.escalation["reason"] == "turn_limit" and "maximum number of messages" in state.escalation["handoff_summary"]
    assert "maximum length" in reply
    assert backend.replies_requested == replies_before  # fixed wording: no model call
    assert not [e for e in trace["events"] if e["kind"] == "llm"]  # and the message was not read
    assert state.messages[-2] == {"role": "user", "text": "And my date of birth is 1985-03-15.", "turn": 3}

    frozen = state.to_record()
    with pytest.raises(ConversationClosed):
        agent.handle_turn(state, "Hello?", backend)
    assert state.to_record() == frozen  # refused before anything changed


def test_an_earlier_handoff_is_kept_at_the_message_cap(repo, settings, backend):
    from .conftest import reading

    agent = Agent(repo, dataclasses.replace(settings, max_turns_per_conversation=3))
    state = agent.new_session()
    backend.next_reading = reading(requests_human=True)
    agent.handle_turn(state, "Get me a person.", backend)
    for text in ("Hello?", "Anyone?"):
        agent.handle_turn(state, text, backend)
    assert state.turn == 3 and state.escalation["reason"] == "caller_requested"
    with pytest.raises(ConversationClosed):
        agent.handle_turn(state, "Still there?", backend)


def test_over_http_the_cap_hands_off_and_then_answers_409(serve, monkeypatch):
    serve()
    capped = Agent(server.repo, dataclasses.replace(server.agent.settings, max_turns_per_conversation=2))
    monkeypatch.setattr(server, "agent", capped)
    client = TestClient(server.app)
    assert client.get("/api/config").json()["limits"]["max_turns_per_conversation"] == 2
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    url = f"/api/sessions/{sid}/messages"
    assert client.post(url, json={"text": "What is RL?"}).json()["state"]["status"] == "active"
    out = client.post(url, json={"text": "What is RL?"}).json()
    assert out["state"]["status"] == "escalated" and out["state"]["turn"] == 2
    before = snapshot(client, sid)
    r = client.post(url, json={"text": "Hello?"})
    assert r.status_code == 409 and envelope(r) == "conversation_closed"
    assert snapshot(client, sid) == before
    assert client.get("/api/stats").json()["counters"]["closed_at_message_limit_409"] == 1


def test_the_default_cap_is_60_and_does_not_touch_a_normal_conversation(settings):
    assert settings.max_turns_per_conversation == 60


# ---------------------------------------------------------------------------
# /api/stats and the turn log
# ---------------------------------------------------------------------------
def test_stats_report_load_without_conversation_content(serve, caplog):
    serve()
    client = TestClient(server.app)
    caplog.set_level(logging.INFO, logger="sop.turn")
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    out = client.post(f"/api/sessions/{sid}/messages", json={"text": DEMO}).json()
    assert out["state"]["phase"] == "PROCESS_CASE"
    client.post(f"/api/sessions/{sid}/messages", json={"text": "What is RL?"})

    stats = client.get("/api/stats").json()
    assert set(stats) == {"uptime_seconds", "process", "store", "turn_slots", "counters", "turn_latency_ms", "limits"}
    assert stats["store"]["kind"] == "memory" and stats["store"]["sessions_held"] == 1
    assert stats["store"]["shared_between_processes"] is False
    assert stats["turn_slots"] == {"in_use": 0, "max": server.settings.max_concurrent_turns, "waiting": 0}
    counters = stats["counters"]
    assert counters["conversations_created"] == 1 and counters["turns_answered"] == 2
    assert all(counters[name] == 0 for name in server.COUNTERS if name not in {"conversations_created", "turns_answered"})
    assert "this process" in counters["scope"]
    latency = stats["turn_latency_ms"]
    assert latency["turns"] == 2 and 0 <= latency["median_ms"] <= latency["p95_ms"]
    assert stats["limits"]["max_body_bytes"] == 16 * 1024 and stats["limits"]["rate_limit_per_minute"] is None
    blob = json.dumps(stats)
    assert sid not in blob and sid[:8] not in blob and "Margaret" not in blob and "4472" not in blob

    lines = [r.getMessage() for r in caplog.records if r.name == "sop.turn"]
    assert len(lines) == 2
    assert lines[0].startswith(f"turn {sid[:8]} phase=VERIFY_ID->PROCESS_CASE status=200 latency=")
    assert "model_calls=" in lines[0]
    for line in lines:
        assert sid not in line and "Margaret" not in line and "1985" not in line and "RL" not in line


def test_refused_and_failed_turns_are_logged_with_their_code(serve, caplog):
    serve()
    client = TestClient(server.app)
    caplog.set_level(logging.INFO, logger="sop.turn")
    client.post("/api/sessions/0123456789abcdef0123456789abcdef/messages", json={"text": "hi"})
    (line,) = [r.getMessage() for r in caplog.records if r.name == "sop.turn"]
    assert line.startswith("turn 01234567 phase=?->? status=404 session_not_found latency=")


def test_sqlite_stats_say_what_is_shared(serve):
    serve("sqlite")
    client = TestClient(server.app)
    client.post("/api/sessions", json={})
    store = client.get("/api/stats").json()["store"]
    assert store["kind"] == "sqlite" and store["shared_between_processes"] is True
    assert store["sessions_held"] == 1 and "every process" in store["scope"]


# ---------------------------------------------------------------------------
# The offline double's simulated latency (load tests only)
# ---------------------------------------------------------------------------
def test_mock_latency_is_off_unless_asked_for(monkeypatch, settings):
    assert settings.mock_latency_ms == 0 and MockBackend().latency_s == 0
    slow = MockBackend(latency_ms=40)
    state = server.agent.new_session()
    started = time.perf_counter()
    slow.understand(state, "What is RL?", settings)
    assert time.perf_counter() - started >= 0.035

    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, mock_latency_ms=25))
    monkeypatch.setattr(server, "_backends", {})
    assert server.get_backend(None).latency_s == pytest.approx(0.025)


# ---------------------------------------------------------------------------
# Several workers, and the logs they write
# ---------------------------------------------------------------------------
def test_several_workers_need_a_shared_store(monkeypatch, capsys):
    from insurance_claims import __main__ as cli

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setenv("SOP_SESSION_STORE", "memory")
    monkeypatch.setenv("SOP_WORKERS", "3")
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2 and not calls
    assert "SOP_SESSION_STORE=sqlite:///" in capsys.readouterr().err

    cli.main(["--workers", "1"])  # one worker is fine with the memory store
    monkeypatch.setenv("SOP_SESSION_STORE", "sqlite:///data/sessions.db")
    cli.main([])
    assert [(c["workers"], c["proxy_headers"]) for c in calls] == [(1, False), (3, False)]

    monkeypatch.setenv("SOP_SESSION_STORE", "redis://cache:6379")
    with pytest.raises(SystemExit):
        cli.main([])


def test_access_log_lines_show_only_the_start_of_a_conversation_id():
    script = (
        "import logging, logging.config\n"
        "from insurance_claims.__main__ import logging_config\n"
        "logging.config.dictConfig(logging_config('INFO'))\n"
        "logging.getLogger('uvicorn.access').info('%s - \"%s %s HTTP/%s\" %d', '127.0.0.1:5000', 'POST',"
        " '/api/sessions/0123456789abcdef0123456789abcdef/messages', '1.1', 200)\n"
        "logging.getLogger('sop.turn').info('turn 01234567 phase=VERIFY_ID->VERIFY_ID status=200')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], cwd=PROJECT_DIR, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert '"POST /api/sessions/01234567.../messages HTTP/1.1" 200' in done.stdout
    assert "0123456789abcdef" not in done.stdout + done.stderr
    assert "INFO:sop.turn:turn 01234567" in done.stderr


# ---------------------------------------------------------------------------
# Many conversations at once on one app: no interference
# ---------------------------------------------------------------------------
def _scripts(fixtures: dict[str, Any]) -> list[list[str]]:
    scripts = [
        [DEMO, "How do I submit the missing documents?", "That's all I needed, thanks.", "Yes, please send it."],
        ["I'm calling about my denied healthcare claim from January.",
         "My name is Margaret Chen and my date of birth is 1985-03-15.", "My phone number is 650-521-2836."],
        ["What is RL?", "Come on, just explain reinforcement learning.", "What's the weather today?", "Tell me a joke."],
    ]
    for p in fixtures["policyholders"]:
        scripts.append([f"My name is {p['name']}. My date of birth is {p['dob']}. The last four of my ID are {p['id_last4']}.",
                        "What is the status of my claim?"])
    return scripts


@pytest.mark.parametrize("store", ["memory", "sqlite"])
def test_many_conversations_at_once_do_not_interfere(serve, store):
    serve(store)
    with TestClient(server.app) as client:
        scripts = _scripts(client.get("/api/fixtures").json())

        def run(script: list[str], sid: str | None = None) -> tuple[str, list[dict[str, Any]]]:
            sid = sid or client.post("/api/sessions", json={}).json()["session_id"]
            for n, text in enumerate(script, start=1):
                r = client.post(f"/api/sessions/{sid}/messages", json={"text": text})
                assert r.status_code == 200, r.text
                assert r.json()["state"]["turn"] == n  # moved by exactly one
            return sid, client.get(f"/api/sessions/{sid}").json()["messages"]

        expected = [run(script)[1] for script in scripts]  # one at a time first

        per_script, threads = 10, 16
        jobs = [i % len(scripts) for i in range(per_script * len(scripts))]
        sids = [client.post("/api/sessions", json={}).json()["session_id"] for _ in jobs]

        def worker(k: int) -> None:
            mine = list(range(k, len(jobs), threads))
            for step in range(max(len(s) for s in scripts)):  # interleave this thread's conversations
                for i in mine:
                    script = scripts[jobs[i]]
                    if step < len(script):
                        r = client.post(f"/api/sessions/{sids[i]}/messages", json={"text": script[step]})
                        assert r.status_code == 200, r.text
                        assert r.json()["state"]["turn"] == step + 1

        with ThreadPoolExecutor(threads) as pool:
            for f in [pool.submit(worker, k) for k in range(threads)]:
                f.result()

        for i, sid in enumerate(sids):
            got = client.get(f"/api/sessions/{sid}").json()
            assert got["messages"] == expected[jobs[i]], f"conversation {i} (script {jobs[i]}) differs"
            assert got["state"]["turn"] == len(scripts[jobs[i]])
        stats = client.get("/api/stats").json()
        assert stats["store"]["sessions_held"] == len(scripts) + len(sids)
        sequential, concurrent = sum(len(s) for s in scripts), sum(len(scripts[j]) for j in jobs)
        assert stats["counters"]["turns_answered"] == sequential + concurrent
        assert stats["counters"]["conflicts_409"] == 0 and stats["counters"]["refused_busy_503"] == 0
