"""Test fixtures. Everything here runs offline: the model is replaced by a scripted
backend, so the tests pin down the harness (gates, memory, transitions, tools,
guard) independently of any model's behaviour."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ["SOP_LLM_MODE"] = "mock"
os.environ["SOP_TODAY"] = "2026-03-02"
# Pin the models too, so a developer's .env cannot change what the wire tests expect.
os.environ["SOP_MODEL"] = "claude-opus-5-5"
os.environ["SOP_MODEL_UNDERSTAND"] = "claude-haiku-5-5"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from insurance_claims.agent import Agent  # noqa: E402
from insurance_claims.config import load_settings  # noqa: E402
from insurance_claims.data import Repository  # noqa: E402
from insurance_claims.mock_llm import MockBackend  # noqa: E402
from insurance_claims.state import SessionState  # noqa: E402


def reading(**overrides: Any) -> dict[str, Any]:
    """A raw 'understanding' as the model would return it, with neutral defaults.

    Shortcuts: identity fields, role/rep_name/relationship and claim hint keys can be
    passed flat, e.g. reading(full_name="Margaret Chen", status="denied").
    """
    identity_keys = ("full_name", "dob", "phone", "email", "id_last4", "policy_number")
    hint_keys = ("case_id", "case_type", "status", "time_reference", "description")
    raw: dict[str, Any] = {
        "summary": "test message",
        "identity": {k: "" for k in identity_keys},
        "caller": {"role": "unknown", "representative_name": "", "relationship": ""},
        "claim_hints": {k: "" for k in hint_keys},
        "intent": "none",
        "scope": "in_scope",
        "out_of_scope_topic": "",
        "emotion": "neutral",
        "emotion_intensity": "none",
        "refusal": "none",
        "refused_fields": [],
        "asks_why": False,
        "requests_human": False,
        "wants_to_end": False,
        "wants_different_claim": False,
        "yes_no": "none",
        "email_summary_choice": "none",
        "email_to_other_address": False,
        "notes": [],
    }
    for key, value in overrides.items():
        if key in identity_keys:
            raw["identity"][key] = value
        elif key in hint_keys:
            raw["claim_hints"][key] = value
        elif key == "role":
            raw["caller"]["role"] = value
        elif key == "rep_name":
            raw["caller"]["representative_name"] = value
        elif key == "relationship":
            raw["caller"]["relationship"] = value
        else:
            raw[key] = value
    return raw


class ScriptedBackend(MockBackend):
    """Understanding comes from the test; claim resolution, summaries and reply
    wording use the deterministic mock."""

    name = "scripted"

    def __init__(self) -> None:
        self.next_reading: dict[str, Any] | None = None
        self.forced_reply: str | None = None
        self.replies_requested = 0

    def understand(self, state, user_text, settings):  # type: ignore[override]
        if self.next_reading is None:
            return super().understand(state, user_text, settings)
        raw, self.next_reading = self.next_reading, None
        return raw, {"call": "understand", "model": "scripted"}

    def respond(self, state, directive, control_block, tool_specs, tool_executor, settings):  # type: ignore[override]
        self.replies_requested += 1
        self.last_control_block = control_block
        self.last_tool_specs = tool_specs
        result = super().respond(state, directive, control_block, tool_specs, tool_executor, settings)
        if self.forced_reply is not None:
            result.text = self.forced_reply
        return result


class Conversation:
    def __init__(self, agent: Agent, backend: ScriptedBackend, scenario: str = "default") -> None:
        self.agent = agent
        self.backend = backend
        self.state: SessionState = agent.new_session(scenario)
        self.last_trace: dict[str, Any] = {}

    def say(self, text: str = "(test)", **read: Any) -> str:
        """Send a caller message. With keyword arguments the understanding is scripted;
        without, the mock's regex reading of `text` is used."""
        if read:
            self.backend.next_reading = reading(**read)
        reply, self.last_trace = self.agent.handle_turn(self.state, text, self.backend)
        return reply

    def events(self, kind: str) -> list[str]:
        return [e["label"] for e in self.last_trace["events"] if e["kind"] == kind]

    @property
    def action(self) -> str:
        return self.events("directive")[-1]


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(scope="session")
def repo():
    return Repository.load()


@pytest.fixture()
def backend():
    return ScriptedBackend()


@pytest.fixture()
def agent(repo, settings):
    return Agent(repo, settings)


@pytest.fixture()
def convo(agent, backend):
    return Conversation(agent, backend)


@pytest.fixture()
def new_convo(agent, backend):
    def make(scenario: str = "default") -> Conversation:
        return Conversation(agent, backend, scenario)

    return make


MARGARET = {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}


@pytest.fixture()
def verified(convo):
    """A conversation where Margaret Chen (P9) has just been verified with no stated need."""
    convo.say("id", **MARGARET)
    assert convo.state.verified
    return convo
