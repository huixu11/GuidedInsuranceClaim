"""The suggested replies of the test UI (`sample_prompts` in /api/config, SAMPLE_PROMPTS in server.py).

The UI offers, for the phase a conversation is in and the point it has reached there, the
suggestions tagged for it, and marks one as the next step on the happy path. These tests pin the
tagging down, and check in mock mode that each suggestion does what it is there to show at every
point where it is offered.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from insurance_claims import server

PROMPTS = server.SAMPLE_PROMPTS
MOMENTS = {"consent", "email_offer", "wrapped_up"}


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


def shown(phase: str, moment: str | None) -> list[dict[str, Any]]:
    """What the UI offers (app.js `currentSuggestions`): the suggestions tagged for this point
    of the phase, else the phase's general ones."""
    in_phase = [p for p in PROMPTS if phase in p["phases"]]
    specific = [p for p in in_phase if moment in p.get("when", [])] if moment else []
    return specific or [p for p in in_phase if not p.get("when")]


def moment_of(state: dict[str, Any]) -> str | None:
    """The point a conversation has reached in its phase (app.js `suggestionMoment`)."""
    if state["status"] == "completed":
        return "wrapped_up"
    if state["phase"] == "POST_PROCESS" and state["email"]["offered"] and not state["email"]["decision"]:
        return "email_offer"
    if state["phase"] == "VERIFY_ID" and state["verification"]["consent"]["status"] == "pending":
        return "consent"
    return None


def text_of(label: str, phase: str) -> str:
    return next(p["text"] for p in PROMPTS if p["label"] == label and phase in p["phases"])


# Every point of the SOP where suggestions are offered, and how to get there with them.
STAGES: dict[tuple[str, str | None], list[tuple[str, str]]] = {
    ("VERIFY_ID", None): [],
    ("VERIFY_ID", "consent"): [("Representative", "VERIFY_ID")],
    ("RESOLVE_INTENT", None): [("Partial ID", "VERIFY_ID"), ("Phone + email", "VERIFY_ID")],
    ("PROCESS_CASE", None): [("Demo test case", "VERIFY_ID")],
    ("POST_PROCESS", "email_offer"): [("Demo test case", "VERIFY_ID"), ("Wrap up", "PROCESS_CASE")],
    ("POST_PROCESS", "wrapped_up"): [
        ("Demo test case", "VERIFY_ID"),
        ("Wrap up", "PROCESS_CASE"),
        ("Send the summary", "POST_PROCESS"),
    ],
}

Check = Callable[[dict[str, Any], str], bool]


def _escalated(s: dict[str, Any], reply: str) -> bool:
    return s["status"] == "escalated"


def _one_strike(phase: str) -> Check:
    return lambda s, r: s["phase"] == phase and s["guardrails"]["out_of_scope"]["consecutive"] == 1


def _hints(s: dict[str, Any]) -> set[str]:
    return {h["key"] for h in s["memory"]["claim_hints"]}


# What each suggestion should do where it is offered: (phase, moment, label) -> check(state, reply).
EXPECTED: dict[tuple[str, str | None, str], Check] = {
    ("VERIFY_ID", None, "Demo test case"): lambda s, r: (
        s["phase"] == "PROCESS_CASE" and s["case"]["selected_case_id"] == "CL-2048"
    ),
    ("VERIFY_ID", None, "Intent before ID"): lambda s, r: (
        s["phase"] == "VERIFY_ID" and _hints(s) >= {"status", "case_type", "time_reference"}
    ),
    ("VERIFY_ID", None, "Partial ID"): lambda s, r: s["verification"]["matched_count"] == 2,
    ("VERIFY_ID", None, "Phone + email"): lambda s, r: s["verification"]["matched_count"] == 2,
    # The SSN is declined: other identifiers are offered and it is not asked for.
    ("VERIFY_ID", None, "Won't share SSN"): lambda s, r: (
        s["verification"]["matched_count"] == 1 and "SSN" not in r and "date of birth" in r
    ),
    ("VERIFY_ID", None, "Frustrated caller"): lambda s, r: (
        s["guardrails"]["emotion"]["label"] == "frustrated"
        and s["guardrails"]["refusals"]["count"] == 1
        and not s["verification"]["verified"]
    ),
    ("VERIFY_ID", None, "Representative"): lambda s, r: (
        s["verification"]["caller_role"] == "representative" and moment_of(s) == "consent"
    ),
    ("VERIFY_ID", None, "Out of scope"): _one_strike("VERIFY_ID"),
    ("VERIFY_ID", None, "Ask for a human"): _escalated,
    ("VERIFY_ID", "consent", "Consent approved"): lambda s, r: (
        s["verification"]["verified"] and s["verification"]["consent"]["status"] == "approved"
    ),
    ("RESOLVE_INTENT", None, "Name the claim"): lambda s, r: (
        s["phase"] == "PROCESS_CASE" and s["case"]["selected_case_id"] == "CL-2048"
    ),
    ("RESOLVE_INTENT", None, "Out of scope"): _one_strike("RESOLVE_INTENT"),
    ("RESOLVE_INTENT", None, "Ask for a human"): _escalated,
    ("PROCESS_CASE", None, "Document follow-up"): lambda s, r: (
        s["phase"] == "PROCESS_CASE" and s["case"]["intent"] == "document_submission" and "upload" in r
    ),
    ("PROCESS_CASE", None, "Why denied"): lambda s, r: (
        s["case"]["intent"] == "denial_question" and "denied because" in r
    ),
    ("PROCESS_CASE", None, "Different claim"): lambda s, r: (
        s["phase"] == "PROCESS_CASE"
        and s["case"]["selected_case_id"] == "CL-2102"
        and any(h["to"] == "RESOLVE_INTENT" for h in s["phase_history"][1:])
    ),
    ("PROCESS_CASE", None, "Frustrated caller"): lambda s, r: (
        s["phase"] == "PROCESS_CASE" and s["guardrails"]["emotion"]["label"] == "frustrated"
    ),
    ("PROCESS_CASE", None, "Wrap up"): lambda s, r: moment_of(s) == "email_offer",
    ("PROCESS_CASE", None, "Out of scope"): _one_strike("PROCESS_CASE"),
    ("PROCESS_CASE", None, "Ask for a human"): _escalated,
    ("POST_PROCESS", "email_offer", "Send the summary"): lambda s, r: (
        s["status"] == "completed" and len(s["email"]["outbox"]) == 1
    ),
    ("POST_PROCESS", "email_offer", "Skip the summary"): lambda s, r: (
        s["status"] == "completed" and s["email"]["decision"] == "skip" and not s["email"]["outbox"]
    ),
    # Declined (only the address on file is used) and asked again; not an off-topic strike.
    ("POST_PROCESS", "email_offer", "Work address instead"): lambda s, r: (
        moment_of(s) == "email_offer"
        and not s["email"]["outbox"]
        and s["guardrails"]["out_of_scope"]["total"] == 0
        and "on file" in r
    ),
    ("POST_PROCESS", "email_offer", "One more question"): lambda s, r: (
        s["phase"] == "PROCESS_CASE" and "appeal deadline" in r
    ),
    ("POST_PROCESS", "wrapped_up", "One more question"): lambda s, r: (
        s["phase"] == "PROCESS_CASE" and s["status"] == "active" and "appeal deadline" in r
    ),
}

OFFERED = [(phase, moment, p["label"]) for (phase, moment) in STAGES for p in shown(phase, moment)]


def test_config_serves_the_tagged_suggestions(client):
    cfg = client.get("/api/config").json()
    assert cfg["sample_prompts"] == PROMPTS
    phases = {p["id"] for p in cfg["phases"]}
    for p in PROMPTS:
        assert p["label"].strip() and p["text"].strip()
        assert p["phases"] and set(p["phases"]) <= phases
        assert set(p.get("when", [])) <= MOMENTS
        assert isinstance(p.get("happy_path", False), bool)
        if "then" in p:
            assert p.get("happy_path") and any(
                q["label"] == p["then"] for phase in p["phases"] for q in shown(phase, None)
            )
    assert {phase for phase, _ in STAGES} == phases


def test_every_phase_has_suggestions_and_one_next_step():
    for phase, moment in STAGES:
        listed = shown(phase, moment)
        assert listed, (phase, moment)
        happy = [p["label"] for p in listed if p.get("happy_path")]
        if moment == "wrapped_up":
            assert happy == [], "a completed conversation has no next step, only follow-ups"
        else:
            assert len(happy) == 1, (phase, moment, happy)
        assert len({p["label"] for p in listed}) == len(listed), "labels repeat at one point"


def test_every_offered_suggestion_has_an_expectation():
    assert set(OFFERED) == set(EXPECTED)


@pytest.mark.parametrize("phase,moment,label", OFFERED)
def test_suggestion_does_what_it_is_there_to_show(client, phase, moment, label):
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    state = client.get(f"/api/sessions/{sid}").json()["state"]
    for step_label, step_phase in STAGES[(phase, moment)]:
        state = client.post(
            f"/api/sessions/{sid}/messages", json={"text": text_of(step_label, step_phase)}
        ).json()["state"]
    assert (state["phase"], moment_of(state)) == (phase, moment), "the stage setup drifted"

    out = client.post(f"/api/sessions/{sid}/messages", json={"text": text_of(label, phase)}).json()
    assert EXPECTED[(phase, moment, label)](out["state"], out["reply"]), out["reply"]


def test_following_the_marked_suggestions_runs_the_demo(client):
    """Clicking the marked next step each time: greeting -> verified claim -> documents
    question -> wrap-up -> summary sent, which is the four-message demo."""
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    state = client.get(f"/api/sessions/{sid}").json()["state"]
    sent: list[tuple[int, str]] = []  # (turn, text) of each caller message
    path = []
    while state["status"] == "active" and len(path) < 6:
        phase = state["phase"]
        listed = shown(phase, moment_of(state))
        entered = max(h["turn"] for h in state["phase_history"] if h["to"] == phase)
        said_here = {text for turn, text in sent if turn > entered}
        step = next(p for p in listed if p.get("happy_path"))
        if step["text"] in said_here and step.get("then"):
            step = next(p for p in listed if p["label"] == step["then"])
        out = client.post(f"/api/sessions/{sid}/messages", json={"text": step["text"]}).json()
        state = out["state"]
        sent.append((state["turn"], step["text"]))
        path.append(step["label"])
    assert path == ["Demo test case", "Document follow-up", "Wrap up", "Send the summary"]
    assert state["status"] == "completed" and len(state["email"]["outbox"]) == 1
