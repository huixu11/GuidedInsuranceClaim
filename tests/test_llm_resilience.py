"""What the Claude backend does when the model service fails: timeouts, the turn budget and the
circuit breaker. No network: the SDK gets a mock HTTP transport, the breaker a clock the test moves.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import anthropic
import httpx2
import pytest

from insurance_claims import llm
from insurance_claims.agent import Agent
from insurance_claims.llm import AnthropicBackend, CircuitBreaker, LLMError, model_health, turn_budget

from .test_llm_wire import DEMO, FakeAPI, demo_reading


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock():
    return Clock()


# ---------------------------------------------------------------------------
# The breaker by itself
# ---------------------------------------------------------------------------
def test_breaker_opens_after_failures_in_a_row_and_only_then(clock):
    b = CircuitBreaker(3, 15.0, clock=clock)
    for _ in range(2):
        assert b.admit() == 0.0
        b.record(False)
    assert b.snapshot()["state"] == "closed"
    b.record(True)  # an answer in between: the count starts again
    for _ in range(2):
        b.record(False)
    assert b.admit() == 0.0 and b.snapshot()["consecutive_failures"] == 2
    b.record(False)
    assert b.snapshot()["state"] == "open" and b.opened_total == 1
    assert b.admit() == pytest.approx(15.0)
    clock.now += 10
    assert b.admit() == pytest.approx(5.0)
    assert b.refused_total == 2


def test_breaker_lets_one_probe_through_and_its_success_closes_it(clock):
    b = CircuitBreaker(1, 15.0, clock=clock)
    b.record(False)
    clock.now += 15
    assert b.snapshot()["state"] == "half_open"
    assert b.admit() == 0.0  # the probe
    assert b.admit() == 1.0  # everyone else waits for its result
    assert b.admit() == 1.0
    b.record(True)
    assert b.snapshot()["state"] == "closed" and b.snapshot()["consecutive_failures"] == 0
    assert b.admit() == 0.0 and b.admit() == 0.0


def test_breaker_opens_again_when_the_probe_fails(clock):
    b = CircuitBreaker(1, 15.0, clock=clock)
    b.record(False)
    clock.now += 20
    assert b.admit() == 0.0
    b.record(False)
    assert b.snapshot()["state"] == "open"
    assert b.admit() == pytest.approx(15.0)  # a full open period from the failed probe
    assert b.opened_total == 1  # one outage, however many probes it takes


def test_a_probe_that_never_reports_is_given_up(clock):
    b = CircuitBreaker(1, 15.0, probe_seconds=60.0, clock=clock)
    b.record(False)
    clock.now += 15
    assert b.admit() == 0.0
    clock.now += 59
    assert b.admit() == 1.0
    clock.now += 1
    assert b.admit() == 0.0  # a new probe


def test_abandoning_the_probe_lets_another_through(clock):
    b = CircuitBreaker(1, 15.0, clock=clock)
    b.record(False)
    clock.now += 15
    assert b.admit() == 0.0
    b.abandon()
    assert b.snapshot()["state"] == "half_open"
    assert b.admit() == 0.0


def test_late_failures_of_calls_admitted_earlier_do_not_extend_the_open_period(clock):
    b = CircuitBreaker(2, 15.0, clock=clock)
    b.record(False)
    b.record(False)
    clock.now += 10
    b.record(False)  # a call that was already under way when the breaker opened
    assert b.admit() == pytest.approx(5.0)
    assert b.opened_total == 1


def test_breaker_can_be_switched_off(clock):
    b = CircuitBreaker(0, 15.0, clock=clock)
    for _ in range(50):
        b.record(False)
        assert b.admit() == 0.0
    assert b.snapshot() == {
        "state": "closed", "enabled": False, "consecutive_failures": 0, "opens_after_failures": 0,
        "open_seconds": 15.0, "retry_in_seconds": 0.0, "opened_since_start": 0, "calls_refused_since_start": 0,
    }


# ---------------------------------------------------------------------------
# The backend with a failing service
# ---------------------------------------------------------------------------
class FailingAPI(FakeAPI):
    """FakeAPI that can fail in the ways a model service does."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: Any = None  # a status code, "connect" or "timeout"
        self.fail_kinds: set[str] | None = None  # only these calls fail (None: all)
        self.headers: dict[str, str] = {}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if self.fail is None:
            return super().handler(request)
        body = json.loads(request.content)
        system = body["system"][0]["text"]
        kind = (
            "understand" if system.startswith("You are the language-understanding")
            else "resolve" if system.startswith("You are the claim-matching")
            else "summarize" if system.startswith("You draft the content")
            else "respond"
        )
        if self.fail_kinds is not None and kind not in self.fail_kinds:
            return super().handler(request)
        self.requests.append({"kind": kind, "extensions": dict(request.extensions), "body": body,
                              "path": str(request.url), "headers": dict(request.headers)})
        if self.fail == "connect":
            raise httpx2.ConnectError("connection refused", request=request)
        if self.fail == "timeout":
            raise httpx2.ReadTimeout("timed out", request=request)
        kinds = {401: "authentication_error", 429: "rate_limit_error", 529: "overloaded_error"}
        return httpx2.Response(
            self.fail,
            headers=self.headers,
            json={"type": "error", "error": {"type": kinds.get(self.fail, "api_error"), "message": "no"}},
        )


@pytest.fixture()
def api():
    return FailingAPI()


@pytest.fixture()
def live(api, repo, settings, clock):
    cfg = dataclasses.replace(settings, model_breaker_failures=3, model_breaker_seconds=15.0)
    backend = AnthropicBackend(cfg, api_key="sk-ant-test")
    backend.client = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api.handler)),
    )
    backend.breaker = CircuitBreaker(3, 15.0, clock=clock)
    agent = Agent(repo, cfg)
    return agent, backend, agent.new_session()


def fail_turn(live, text: str = "hello") -> LLMError:
    agent, backend, state = live
    working = type(state).from_record(state.to_record())  # a failed turn's copy is thrown away
    with pytest.raises(LLMError) as err:
        agent.handle_turn(working, text, backend)
    return err.value


def test_client_has_the_configured_timeout_and_retries(settings):
    backend = AnthropicBackend(settings, api_key="sk-ant-test")
    assert backend.client.max_retries == settings.model_max_retries == 2
    assert backend.client.timeout.read == settings.model_timeout_seconds == 60.0
    assert backend.client.timeout.connect == 5.0


@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [(500, "llm_error", 502), (502, "llm_error", 502), ("connect", "llm_error", 502), ("timeout", "llm_error", 504),
     (529, "model_overloaded", 503), (503, "model_overloaded", 503)],
)
def test_an_unanswering_service_opens_the_breaker_and_calls_stop(api, live, failure, code, status):
    _, backend, _ = live
    api.fail = failure
    for _ in range(3):
        err = fail_turn(live)
        assert (err.code, err.status) == (code, status)
    assert len(api.requests) == 3 and backend.breaker.snapshot()["state"] == "open"
    err = fail_turn(live)
    assert (err.code, err.status) == ("model_unavailable", 503)
    assert err.retry_after == 15
    assert len(api.requests) == 3  # refused without calling the service


def test_overload_passes_on_the_service_retry_after(api, live):
    api.fail, api.headers = 529, {"retry-after": "7"}
    assert fail_turn(live).retry_after == 7
    api.headers = {}
    assert fail_turn(live).retry_after == 5
    api.headers = {"retry-after": "100000"}
    assert fail_turn(live).retry_after == 60


@pytest.mark.parametrize(("failure", "code", "status"), [(401, "invalid_api_key", 401), (429, "rate_limited", 429)])
def test_an_answer_about_one_key_never_opens_the_breaker(api, live, failure, code, status):
    _, backend, _ = live
    api.fail, api.headers = failure, {"retry-after": "3"}
    for _ in range(10):
        err = fail_turn(live)
        assert (err.code, err.status) == (code, status)
    assert err.retry_after == (3 if failure == 429 else None)
    assert len(api.requests) == 10
    assert backend.breaker.snapshot()["state"] == "closed"
    assert backend.breaker.snapshot()["consecutive_failures"] == 0


def test_an_answer_resets_the_count_of_failures(api, live):
    _, backend, _ = live
    api.fail = 500
    fail_turn(live)
    fail_turn(live)
    api.fail = 401  # the service answers (if only to refuse this key)
    fail_turn(live)
    api.fail = 500
    fail_turn(live)
    fail_turn(live)
    assert backend.breaker.snapshot()["state"] == "closed"


def test_service_comes_back_one_probe_then_everyone(api, live, clock):
    agent, backend, state = live
    api.fail = "connect"
    for _ in range(3):
        fail_turn(live)
    assert fail_turn(live).code == "model_unavailable"
    clock.now += 15
    api.fail = None  # the service is back
    api.understanding = demo_reading()
    reply, _ = agent.handle_turn(state, DEMO, backend)  # the probe, and the turn's later calls
    assert reply == api.reply_text
    assert backend.breaker.snapshot()["state"] == "closed"
    assert state.verified


def test_refused_turn_leaves_no_trace_of_itself_on_the_service_side(api, live):
    """While the breaker is open a turn fails before the SOP engine has decided anything."""
    agent, backend, state = live
    backend.breaker.record(False)
    backend.breaker.record(False)
    backend.breaker.record(False)
    before = json.dumps(state.to_record(), sort_keys=True)
    working = type(state).from_record(state.to_record())  # the copy a turn works on
    with pytest.raises(LLMError) as err:
        agent.handle_turn(working, DEMO, backend)
    assert err.value.code == "model_unavailable"
    assert api.requests == []
    assert json.dumps(state.to_record(), sort_keys=True) == before


def test_failure_while_phrasing_sends_the_template_and_keeps_the_decision(api, live):
    """The reading succeeded and the engine decided; only the reply call fails."""
    agent, backend, state = live
    api.understanding = demo_reading()
    api.fail, api.fail_kinds = 500, {"respond"}
    reply, trace = agent.handle_turn(state, DEMO, backend)
    assert state.verified and reply and reply != api.reply_text
    errors = [e["label"] for e in trace["events"] if e["kind"] == "error"]
    assert any("using the template wording" in label for label in errors)


def test_breaker_is_shared_by_every_key_in_the_process(settings):
    one = AnthropicBackend(settings, api_key="sk-ant-one")
    two = AnthropicBackend(settings, api_key="sk-ant-two")
    assert one.breaker is two.breaker
    health = model_health()
    assert health["state"] == "closed" and health["opens_after_failures"] == settings.model_breaker_failures
    assert "sk-ant" not in json.dumps(health)
    llm.reset_model_health()
    assert model_health()["state"] == "closed" and AnthropicBackend(settings, api_key="k").breaker is not one.breaker


# ---------------------------------------------------------------------------
# The turn budget
# ---------------------------------------------------------------------------
def test_inside_a_turn_budget_each_call_gets_no_more_than_the_time_left(api, live):
    agent, backend, state = live
    api.understanding = demo_reading()
    seen: list[dict[str, Any]] = []
    original = api.handler

    def spy(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions.get("timeout") or {}))
        return original(request)

    backend.client = anthropic.Anthropic(
        api_key="sk-ant-test", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(spy)),
    )
    with turn_budget(30.0):
        agent.handle_turn(state, DEMO, backend)
    assert len(seen) >= 3
    for timeout in seen:
        assert 0 < timeout["read"] <= 30.0 and 0 < timeout["connect"] <= 5.0
    # Outside a budget (the command line, scripts) the client's own timeout applies.
    seen.clear()
    backend._create(model=backend.settings.model_understand, max_tokens=10, system=[{"type": "text", "text": "x"}],
                    messages=[{"role": "user", "content": "x"}])
    assert seen[0]["read"] == 600.0 or seen[0]["read"] == backend.client.timeout.read


def test_no_call_starts_once_the_turn_budget_is_spent(api, live):
    agent, backend, state = live
    with turn_budget(0.5):
        with pytest.raises(LLMError) as err:
            agent.handle_turn(state, DEMO, backend)
    assert (err.value.code, err.value.status) == ("turn_timeout", 504)
    assert api.requests == []
    assert backend.breaker.snapshot()["consecutive_failures"] == 0  # says nothing about the service


def test_turn_budget_ends_with_its_block(api, live):
    _, backend, _ = live
    with turn_budget(0.0):
        with pytest.raises(LLMError):
            backend._attempt_timeout()
    assert backend._attempt_timeout() is None


def test_settings_keep_the_hold_on_a_conversation_longer_than_the_longest_turn(monkeypatch):
    from insurance_claims.config import load_settings

    cfg = load_settings()
    assert cfg.turn_max_seconds >= cfg.turn_budget_seconds + cfg.model_max_retries * cfg.model_timeout_seconds
    monkeypatch.setenv("SOP_TURN_MAX_SECONDS", "30")
    monkeypatch.setenv("SOP_TURN_BUDGET_SECONDS", "200")
    monkeypatch.setenv("SOP_MODEL_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("SOP_MODEL_MAX_RETRIES", "3")
    cfg = load_settings()
    assert cfg.turn_max_seconds >= 200 + 3 * 90
    monkeypatch.setenv("SOP_MODEL_BREAKER_FAILURES", "0")
    monkeypatch.setenv("SOP_MODEL_TIMEOUT_SECONDS", "1")
    cfg = load_settings()
    assert cfg.model_breaker_failures == 0 and cfg.model_timeout_seconds == 5.0
