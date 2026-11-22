"""The SOP, as data.

This module is the single place that states what each phase is for, how much
freedom the LLM gets in it, what may be disclosed, which tools exist there and
who may call them, and which phase transitions are legal. The policy engine
(`policy.py`) drives the conversation, but every transition and every tool call
is checked against this specification, so a bug or a persuasive caller cannot
take a path the SOP does not define.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .state import Phase, SessionState

# Freedom levels: how tightly the harness scripts the LLM's reply.
STRICT = "strict"  # harness dictates the move; LLM only phrases it. No data, no tools.
GUIDED = "guided"  # LLM chooses among bounded options the harness validates.
OPEN = "open"  # LLM reasons and answers freely, but only from grounded data/tools.

# Disclosure levels: what record data is placed in the LLM's context at all.
DISCLOSE_NONE = "none"
DISCLOSE_INDEX = "claim_index"
DISCLOSE_DETAIL = "claim_detail"

HARNESS = "harness"
LLM = "llm"


class SOPViolation(RuntimeError):
    """Raised when code attempts something the SOP does not allow."""


@dataclass(frozen=True)
class PhaseSpec:
    id: Phase
    label: str
    freedom: str
    description: str
    disclosure: str
    harness_tools: frozenset[str]
    llm_tools: frozenset[str]
    transitions: frozenset[Phase]
    exit_gate: str


SOP: dict[Phase, PhaseSpec] = {
    Phase.VERIFY_ID: PhaseSpec(
        id=Phase.VERIFY_ID,
        label="Verify identity",
        freedom=STRICT,
        description=(
            "Confirm the caller's identity with at least 3 of 5 identifiers (full name, "
            "date of birth, phone, email, SSN/ID last 4). Representatives also need the "
            "policyholder's consent. No policy or claim data is disclosed or even loaded."
        ),
        disclosure=DISCLOSE_NONE,
        harness_tools=frozenset(
            {"verify_identity", "lookup_representative", "request_consent", "check_consent_status"}
        ),
        llm_tools=frozenset(),
        transitions=frozenset({Phase.RESOLVE_INTENT}),
        exit_gate="identity verified (>=3 identifiers match one record; consent approved if representative)",
    ),
    Phase.RESOLVE_INTENT: PhaseSpec(
        id=Phase.RESOLVE_INTENT,
        label="Resolve intent",
        freedom=GUIDED,
        description=(
            "Work out which claim the caller means and what they need. The LLM interprets "
            "messy language and remembered hints, but must pick from the caller's own claims "
            "and a fixed list of workflow paths; the harness validates the choice."
        ),
        disclosure=DISCLOSE_INDEX,
        harness_tools=frozenset({"list_claims"}),
        llm_tools=frozenset(),
        transitions=frozenset({Phase.PROCESS_CASE, Phase.POST_PROCESS}),
        exit_gate="a claim belonging to the verified policyholder is selected",
    ),
    Phase.PROCESS_CASE: PhaseSpec(
        id=Phase.PROCESS_CASE,
        label="Process case",
        freedom=OPEN,
        description=(
            "Answer the caller's questions about the selected claim. The LLM reasons freely "
            "but only from the claim record and results of the tools allowed in this phase."
        ),
        disclosure=DISCLOSE_DETAIL,
        harness_tools=frozenset({"get_claim_details", "list_claims"}),
        llm_tools=frozenset({"get_document_guidance", "list_claims"}),
        transitions=frozenset({Phase.RESOLVE_INTENT, Phase.POST_PROCESS}),
        exit_gate="caller has no further questions, or asks about a different claim",
    ),
    Phase.POST_PROCESS: PhaseSpec(
        id=Phase.POST_PROCESS,
        label="Post-process",
        freedom=GUIDED,
        description=(
            "Offer an email summary (what was discussed, claim status/outcome, next steps). "
            "The caller chooses send or skip; the email is sent only on an explicit yes and "
            "only to the address on file."
        ),
        disclosure=DISCLOSE_DETAIL,
        harness_tools=frozenset({"send_summary_email", "get_claim_details"}),
        llm_tools=frozenset(),
        transitions=frozenset({Phase.RESOLVE_INTENT, Phase.PROCESS_CASE}),
        exit_gate="caller chose send or skip",
    ),
}

PHASE_ORDER: tuple[Phase, ...] = (
    Phase.VERIFY_ID,
    Phase.RESOLVE_INTENT,
    Phase.PROCESS_CASE,
    Phase.POST_PROCESS,
)

# Bounded workflow paths the LLM may choose between once a claim is selected.
WORKFLOW_PATHS: dict[str, str] = {
    "status_inquiry": "where the claim stands",
    "denial_question": "why the claim was denied and how to respond or appeal",
    "document_submission": "which documents are needed and how, where and when to send them",
    "next_steps": "what the caller should do next",
    "payment_question": "amounts: allowed, expected and paid",
    "general_claim_question": "any other question about this claim",
}


def _gate_resolve(state: SessionState, owned_case_ids: set[str]) -> bool:
    return state.verified and bool(state.party_id)


def _gate_process(state: SessionState, owned_case_ids: set[str]) -> bool:
    return (
        state.verified
        and bool(state.selected_case_id)
        and state.selected_case_id in owned_case_ids
    )


ENTRY_GATES: dict[Phase, Callable[[SessionState, set[str]], bool]] = {
    Phase.RESOLVE_INTENT: _gate_resolve,
    Phase.PROCESS_CASE: _gate_process,
    Phase.POST_PROCESS: _gate_resolve,
}


def check_transition(state: SessionState, dst: Phase, owned_case_ids: set[str]) -> None:
    """Raise unless moving from the current phase to `dst` is legal right now."""
    spec = SOP[state.phase]
    if dst not in spec.transitions:
        raise SOPViolation(f"transition {state.phase.value} -> {dst.value} is not defined by the SOP")
    gate = ENTRY_GATES.get(dst)
    if gate and not gate(state, owned_case_ids):
        raise SOPViolation(f"entry gate for {dst.value} is not satisfied")


def check_tool(phase: Phase, tool: str, actor: str) -> None:
    """Raise unless `actor` (harness or llm) may call `tool` in `phase`."""
    spec = SOP[phase]
    allowed = spec.llm_tools if actor == LLM else spec.harness_tools
    if tool not in allowed:
        raise SOPViolation(f"tool '{tool}' is not available to the {actor} in {phase.value}")


def phases_for_ui() -> list[dict[str, str]]:
    return [
        {
            "id": SOP[p].id.value,
            "label": SOP[p].label,
            "freedom": SOP[p].freedom,
            "description": SOP[p].description,
        }
        for p in PHASE_ORDER
    ]
