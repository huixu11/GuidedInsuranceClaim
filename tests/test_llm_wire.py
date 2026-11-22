"""Wire-level tests of the Claude backend against a fake Messages API.

No network: the Anthropic SDK is given a mock HTTP transport. These tests pin
down what the backend sends (endpoint, betas, structured-output config, tools,
how tool results and thinking blocks are passed back) and that it handles the
response shapes it will meet (thinking blocks, tool use, refusals, auth errors).
They cannot prove the real API accepts the requests — `scripts/run_scenarios.py`
with a real key does that.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest

from insurance_claims import policy
from insurance_claims.agent import Agent
from insurance_claims.llm import FALLBACK_BETA, AnthropicBackend, LLMError
from insurance_claims.state import Phase

from .conftest import reading

DEMO = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def message(content: list[dict[str, Any]], stop_reason: str = "end_turn", **extra: Any) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        **extra,
    }


class FakeAPI:
    """Answers each call the way the corresponding model call should be answered."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.understanding = reading()
        self.reply_text = "Thanks, Margaret — you're verified. Claim CL-2048 was denied; the appeal deadline is March 18, 2026."
        self.use_tool = False
        self.refuse: set[str] = set()
        self.status: int | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        system = body["system"][0]["text"]
        kind = (
            "understand" if system.startswith("You are the language-understanding")
            else "resolve" if system.startswith("You are the claim-matching")
            else "summarize" if system.startswith("You draft the content")
            else "respond"
        )
        self.requests.append(
            {"kind": kind, "path": str(request.url), "headers": dict(request.headers), "body": body}
        )
        if self.status:
            return httpx2.Response(
                self.status,
                json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
            )
        if kind in self.refuse:
            return httpx2.Response(200, json=message([], "refusal", stop_details={
                "type": "refusal", "category": "cyber", "explanation": "declined"}))
        if kind == "understand":
            return httpx2.Response(200, json=message([THINKING, {"type": "text", "text": json.dumps(self.understanding)}]))
        if kind == "resolve":
            out = {"case_id": "CL-2048", "candidate_case_ids": [], "intent": "denial_question", "rationale": "only denied healthcare claim"}
            return httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(out)}]))
        if kind == "summarize":
            out = {"subject": "Your claim CL-2048", "discussed": ["Why claim CL-2048 was denied"],
                   "claim_status": "Claim CL-2048 is denied.", "next_steps": ["Send the pathology report by March 18, 2026."]}
            return httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(out)}]))
        last = body["messages"][-1]
        answered = isinstance(last["content"], list) and any(b.get("type") == "tool_result" for b in last["content"])
        if self.use_tool and not answered:
            return httpx2.Response(200, json=message([
                THINKING,
                {"type": "tool_use", "id": "toolu_1", "name": "get_document_guidance",
                 "input": {"case_id": "CL-2048", "topic": "submission_method", "document": ""}},
            ], "tool_use"))
        return httpx2.Response(200, json=message([THINKING, {"type": "text", "text": self.reply_text}]))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["kind"] == kind]


@pytest.fixture()
def api():
    return FakeAPI()


@pytest.fixture()
def live(api, repo, settings):
    backend = AnthropicBackend(settings, api_key="sk-ant-test")
    backend.client = anthropic.Anthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api.handler)),
    )
    agent = Agent(repo, settings)
    return agent, backend, agent.new_session()


def demo_reading() -> dict[str, Any]:
    return reading(role="policyholder", full_name="Margaret Chen", dob="1985-03-15", id_last4="4472",
                   policy_number="POL-9921", case_type="healthcare", status="denied",
                   time_reference="January")


def test_demo_turn_makes_three_well_formed_calls(api, live):
    agent, backend, state = live
    api.understanding = demo_reading()
    reply, trace = agent.handle_turn(state, DEMO, backend)

    assert [r["kind"] for r in api.requests] == ["understand", "resolve", "respond"]
    assert state.phase == Phase.PROCESS_CASE and state.selected_case_id == "CL-2048"
    assert reply == api.reply_text  # thinking blocks are skipped; text is read by block type

    for req in api.requests:
        body = req["body"]
        structured = req["kind"] != "respond"
        # Structured reading calls go to the small model; the reply is phrased by the main one.
        assert body["model"] == ("claude-haiku-5-5" if structured else "claude-opus-5-5")
        assert "temperature" not in body and "thinking" not in body  # adaptive thinking; no sampling params
        assert "tool_choice" not in body  # forced tool choice is not used
        assert body["output_config"]["effort"] == "low"
        if structured:
            # Haiku has no server-side refusal fallback, so the opt-in is not sent with it.
            assert "fallbacks" not in body
            assert FALLBACK_BETA not in req["headers"].get("anthropic-beta", "")
        else:
            # Refusal fallback opt-in: beta header + fallbacks parameter.
            assert body["fallbacks"] == "default"
            assert FALLBACK_BETA in req["headers"]["anthropic-beta"]
        assert req["headers"]["authorization"] == "Bearer sk-ant-test"  # SDK 2.x sends keys as Bearer
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert body["messages"][0]["role"] == "user"

    understand = api.of("understand")[0]["body"]
    fmt = understand["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(fmt["schema"]["properties"])
    assert "<caller_message>" in understand["messages"][0]["content"]
    assert "CL-2048" not in json.dumps(understand)  # no record data in the pre-verification reading call

    resolve = api.of("resolve")[0]["body"]
    assert "CL-2048" in resolve["messages"][0]["content"] and "CL-3001" not in resolve["messages"][0]["content"]
    assert "claim status: denied" in resolve["messages"][0]["content"]  # remembered hints are passed

    respond = api.of("respond")[0]["body"]
    assert "output_config" in respond and "format" not in respond["output_config"]
    assert "<control_block>" in respond["system"][1]["text"]
    assert [t["name"] for t in respond["tools"]] == ["get_document_guidance", "list_claims"]
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in respond["tools"])
    assert [m["role"] for m in respond["messages"]] == ["user", "assistant", "user"]


def test_no_tools_and_no_record_data_are_sent_before_verification(api, live, repo):
    agent, backend, state = live
    api.understanding = reading(intent="denial_question", status="denied", emotion="frustrated",
                                emotion_intensity="moderate", refusal="identity_info")
    api.reply_text = "I hear you. I do need to verify your identity before I can share claim details."
    agent.handle_turn(state, "Just tell me why my claim was denied.", backend)
    assert [r["kind"] for r in api.requests] == ["understand", "respond"]
    respond = api.of("respond")[0]["body"]
    assert "tools" not in respond
    sent = json.dumps(respond)
    for claim in repo.claims:
        assert claim["case_id"] not in sent
    for holder in repo.policyholders:
        assert holder.id_last4 not in sent and holder.dob not in sent and holder.email not in sent


def test_tool_loop_passes_results_and_thinking_blocks_back(api, live):
    agent, backend, state = live
    api.understanding = demo_reading()
    agent.handle_turn(state, DEMO, backend)
    api.requests.clear()

    api.use_tool = True
    api.understanding = reading(intent="document_submission")
    api.reply_text = "Use the member portal or the claim upload link so the files stay attached to claim CL-2048."
    reply, trace = agent.handle_turn(state, "How do I submit the missing documents?", backend)

    assert reply == api.reply_text
    first, second = api.of("respond")
    assert first["body"]["system"] == second["body"]["system"]  # prefix unchanged inside the loop
    assert first["body"]["tools"] == second["body"]["tools"]
    assistant, results = second["body"]["messages"][-2:]
    assert assistant["role"] == "assistant"
    assert [b["type"] for b in assistant["content"]] == ["thinking", "tool_use"]
    assert assistant["content"][0]["signature"] == "sig-abc"  # returned exactly as received
    block = results["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "toolu_1"
    assert "member portal" in block["content"]  # the fixture guidance, rendered by the tool
    assert any("get_document_guidance(CL-2048, submission_method)" in e["label"]
               for e in trace["events"] if e["kind"] == "tool")


def test_refused_reply_falls_back_to_the_template(api, live):
    agent, backend, state = live
    api.understanding = demo_reading()
    api.refuse = {"respond"}
    reply, trace = agent.handle_turn(state, DEMO, backend)
    assert "CL-2048" in reply and state.phase == Phase.PROCESS_CASE  # the SOP still made its move
    assert any(e["kind"] == "error" and "template" in e["label"] for e in trace["events"])


def test_refused_reading_is_treated_as_out_of_scope(api, live):
    agent, backend, state = live
    api.refuse = {"understand"}
    api.reply_text = "I can't help with that, but I can help with your claim once you're verified."
    agent.handle_turn(state, "something the classifier declines", backend)
    assert state.oos_consecutive == 1 and not state.verified
    assert state.traces[-1]["events"][-2]["kind"] in {"llm", "directive"}


def test_email_summary_uses_the_summarize_call(api, live):
    agent, backend, state = live
    api.understanding = demo_reading()
    agent.handle_turn(state, DEMO, backend)
    api.understanding = reading(wants_to_end=True)
    api.reply_text = "Would you like an email summary sent to m•••@email.com, or shall I skip it?"
    agent.handle_turn(state, "That's all.", backend)
    assert state.phase == Phase.POST_PROCESS and state.outbox == []
    api.understanding = reading(yes_no="yes")
    api.reply_text = "Done, I've sent it to m•••@email.com. Take care."
    agent.handle_turn(state, "Yes please.", backend)
    assert len(api.of("summarize")) == 1
    assert state.outbox[0]["subject"] == "Your claim CL-2048"
    assert "Send the pathology report by March 18, 2026." in state.outbox[0]["body"]


def test_auth_error_is_mapped_and_nothing_is_committed(api, live):
    agent, backend, state = live
    api.status = 401
    with pytest.raises(LLMError) as err:
        agent.handle_turn(state, "hello", backend)
    assert err.value.code == "invalid_api_key" and err.value.status == 401


def test_gateway_that_rejects_the_fallback_beta_is_tolerated(api, live):
    agent, backend, state = live
    original = api.handler

    def picky(request: httpx2.Request) -> httpx2.Response:
        if "fallbacks" in json.loads(request.content):
            return httpx2.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "fallbacks: Extra inputs are not permitted"}})
        return original(request)

    backend.client = anthropic.Anthropic(
        api_key="sk-ant-test", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(picky)),
    )
    api.understanding = demo_reading()
    reply, _ = agent.handle_turn(state, DEMO, backend)
    assert reply == api.reply_text and backend._fallbacks_rejected
    assert all("fallbacks" not in r["body"] for r in api.requests)
    assert policy.ANSWER_CASE in [e["label"] for e in state.traces[-1]["events"] if e["kind"] == "directive"]
