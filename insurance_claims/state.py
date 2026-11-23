"""Session state: everything the harness knows about one conversation.

The state is owned and mutated only by the deterministic policy engine. The
LLM never writes to it directly; it sees a filtered projection (the control
block) built from it each turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any

from .identity import FIELD_LABELS, MATCHED, NOT_PROVIDED, PII_FIELDS

# Version of the stored record layout (`SessionState.to_record`). Bump it when a field changes
# meaning, so a store can tell an old record from a new one.
RECORD_FORMAT = 1


class Phase(str, Enum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"


STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_ESCALATED = "escalated"

HINT_LABELS = {
    "case_id": "Claim number",
    "case_type": "Claim type",
    "status": "Claim status",
    "time_reference": "Time reference",
    "description": "Details",
}

# SessionState fields holding one MemoryItem or None (to_record / from_record).
_MEMORY_ITEM_FIELDS = ("intent", "email_pref")


@dataclass
class MemoryItem:
    value: str
    captured_turn: int
    captured_phase: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "captured_turn": self.captured_turn,
            "captured_phase": self.captured_phase,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryItem:
        return cls(
            value=data["value"],
            captured_turn=data["captured_turn"],
            captured_phase=data["captured_phase"],
        )


@dataclass
class Trace:
    turn: int
    phase_before: str
    phase_after: str = ""
    latency_ms: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, label: str, detail: Any = None) -> None:
        self.events.append({"kind": kind, "label": label, "detail": detail})

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "phase_before": self.phase_before,
            "phase_after": self.phase_after,
            "latency_ms": self.latency_ms,
            "events": self.events,
        }


@dataclass
class SessionState:
    session_id: str
    consent_scenario: str = "default"
    turn: int = 0
    phase: Phase = Phase.VERIFY_ID
    status: str = STATUS_ACTIVE
    phase_history: list[dict[str, Any]] = field(default_factory=list)

    # --- identity / verification gate ------------------------------------
    identity_claims: dict[str, str] = field(default_factory=dict)  # what the caller supplied
    caller_role: str = "unknown"  # policyholder | representative | unknown
    rep_name: str | None = None
    rep_relationship: str | None = None
    rep_on_file: bool | None = None
    verified: bool = False
    party_id: str | None = None  # set only once verified
    candidate_party_id: str | None = None  # best-matching record while verifying
    field_status: dict[str, str] = field(
        default_factory=lambda: {f: NOT_PROVIDED for f in PII_FIELDS}
    )
    policy_status: str = NOT_PROVIDED
    failed_attempts: int = 0
    seen_mismatches: list[str] = field(default_factory=list)  # "field=normalized value"
    declined_fields: list[str] = field(default_factory=list)
    consent_required: bool = False
    consent_status: str = "not_required"  # not_required|not_requested|pending|approved|timed_out
    consent_polls: int = 0
    consent_party_id: str | None = None  # the policyholder the consent request was sent to
    consent_rep: str | None = None  # the representative (normalised name) it was requested for
    # Outcome per (policyholder, representative) pair, so switching identifiers back and forth
    # can neither inherit someone else's approval nor restart a request that timed out.
    consent_ledger: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- cross-phase memory ------------------------------------------------
    intent: MemoryItem | None = None
    hints: dict[str, MemoryItem] = field(default_factory=dict)
    notes: list[MemoryItem] = field(default_factory=list)
    email_pref: MemoryItem | None = None

    # --- case ---------------------------------------------------------------
    claims_on_file: int | None = None
    selected_case_id: str | None = None
    selected_case_brief: str | None = None  # "<id> (<type> claim, filed <date>, <status>)"
    case_intent: str | None = None
    candidates: list[str] = field(default_factory=list)
    cases_discussed: list[str] = field(default_factory=list)
    clarify_attempts: int = 0

    # --- guardrails ---------------------------------------------------------
    oos_consecutive: int = 0
    oos_total: int = 0
    emotion_label: str = "neutral"
    emotion_intensity: int = 0
    elevated_streak: int = 0
    refusal_count: int = 0
    human_offer_pending: bool = False
    guard_blocks: int = 0

    # --- escalation / wrap-up ----------------------------------------------
    escalation: dict[str, Any] | None = None
    email_offered: bool = False
    email_decision: str | None = None
    email_reasks: int = 0
    outbox: list[dict[str, Any]] = field(default_factory=list)

    # --- transcript & audit -------------------------------------------------
    messages: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    grounding: list[str] = field(default_factory=list)  # tool/claim data shown to the model

    # -- helpers -------------------------------------------------------------
    @property
    def matched_count(self) -> int:
        return sum(1 for f in PII_FIELDS if self.field_status.get(f) == MATCHED)

    def user_texts(self) -> list[str]:
        return [m["text"] for m in self.messages if m["role"] == "user"]

    def last_assistant_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                return m["text"]
        return ""

    def hint_summary(self) -> str:
        """Human-readable digest of what the caller has said about their claim/need."""
        parts = []
        for key in ("status", "case_type", "case_id", "time_reference", "description"):
            item = self.hints.get(key)
            if item:
                parts.append(f"{HINT_LABELS[key].lower()}: {item.value}")
        if self.intent:
            parts.append(f"wants: {self.intent.value.replace('_', ' ')}")
        return "; ".join(parts)

    # -- storage -------------------------------------------------------------
    def to_record(self) -> dict[str, Any]:
        """Everything in the state as plain JSON data, for a session store.

        Lossless: `SessionState.from_record(json.loads(json.dumps(s.to_record()))) == s`. Unlike
        `to_view` (a display projection) it keeps every field, identifier values included, so a
        record is as sensitive as the conversation itself. The result may share lists and dicts
        with the state; serialise it before keeping it.
        """
        record: dict[str, Any] = {"format": RECORD_FORMAT}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "phase":
                value = value.value
            elif f.name in _MEMORY_ITEM_FIELDS:
                value = value.to_dict() if value is not None else None
            elif f.name == "hints":
                value = {k: item.to_dict() for k, item in value.items()}
            elif f.name == "notes":
                value = [item.to_dict() for item in value]
            record[f.name] = value
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> SessionState:
        """Rebuild a state from `to_record()` output (after a JSON round trip).

        A field missing from the record takes its default, so records written before a field
        was added still load. An unknown field is an error rather than silently dropped, since
        the next commit would otherwise lose it.
        """
        data = dict(record)
        fmt = data.pop("format", RECORD_FORMAT)
        if fmt != RECORD_FORMAT:
            raise ValueError(f"unsupported session record format {fmt!r}")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown fields in session record: {', '.join(unknown)}")
        if "phase" in data:
            data["phase"] = Phase(data["phase"])
        for name in _MEMORY_ITEM_FIELDS:
            if data.get(name) is not None:
                data[name] = MemoryItem.from_dict(data[name])
        if "hints" in data:
            data["hints"] = {k: MemoryItem.from_dict(v) for k, v in data["hints"].items()}
        if "notes" in data:
            data["notes"] = [MemoryItem.from_dict(v) for v in data["notes"]]
        return cls(**data)

    def to_view(self, settings: Any) -> dict[str, Any]:
        """Display-safe projection for the test UI (no identifier values)."""
        rep = None
        if self.caller_role == "representative" or self.rep_name:
            rep = {
                "name": self.rep_name,
                "relationship": self.rep_relationship,
                "on_file": self.rep_on_file,
            }
        return {
            "turn": self.turn,
            "phase": self.phase.value,
            "status": self.status,
            "phase_history": self.phase_history,
            "verification": {
                "verified": self.verified,
                "caller_role": self.caller_role,
                "required_matches": settings.required_id_matches,
                "matched_count": self.matched_count,
                "fields": {
                    f: {"label": FIELD_LABELS[f], "status": self.field_status.get(f, NOT_PROVIDED)}
                    for f in PII_FIELDS
                },
                "policy_number": {
                    "label": FIELD_LABELS["policy_number"],
                    "status": self.policy_status,
                },
                "failed_attempts": self.failed_attempts,
                "max_failed_attempts": settings.max_failed_attempts,
                "representative": rep,
                "consent": {
                    "required": self.consent_required,
                    "status": self.consent_status,
                    "polls": self.consent_polls,
                },
            },
            "memory": {
                "intent": self.intent.to_dict() if self.intent else None,
                "claim_hints": [
                    {"key": k, "label": HINT_LABELS.get(k, k), **v.to_dict()}
                    for k, v in self.hints.items()
                ],
                "notes": [
                    {
                        "text": n.value,
                        "captured_turn": n.captured_turn,
                        "captured_phase": n.captured_phase,
                    }
                    for n in self.notes
                ],
                "email_summary_preference": self.email_pref.to_dict() if self.email_pref else None,
            },
            "case": {
                "claims_on_file": self.claims_on_file,
                "selected_case_id": self.selected_case_id,
                "intent": self.case_intent,
                "candidates": self.candidates,
                "cases_discussed": self.cases_discussed,
            },
            "guardrails": {
                "out_of_scope": {
                    "consecutive": self.oos_consecutive,
                    "total": self.oos_total,
                    "limit": settings.out_of_scope_limit,
                },
                "emotion": {
                    "label": self.emotion_label,
                    "intensity": self.emotion_intensity,
                    "elevated_streak": self.elevated_streak,
                },
                "refusals": {
                    "count": self.refusal_count,
                    "max_persuasion_attempts": settings.max_persuasion_attempts,
                },
                "human_offer_pending": self.human_offer_pending,
                "guard_blocks": self.guard_blocks,
            },
            "escalation": self.escalation,
            "email": {
                "offered": self.email_offered,
                "decision": self.email_decision,
                "outbox": self.outbox,
            },
        }
