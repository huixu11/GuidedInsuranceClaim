"""Session state: everything the harness knows about one conversation.

The state is owned and mutated only by the deterministic policy engine. The
LLM never writes to it directly; it sees a filtered projection (the control
block) built from it each turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any

from .identity import FIELD_LABELS, MATCHED, NOT_PROVIDED, PII_FIELDS

# Version of the stored record layout (`SessionState.to_record`). Bump it when a field changes
# meaning, so a store can tell an old record from a new one.
RECORD_FORMAT = 1

# Lists that only ever grow by appending, one or more entries a turn. A store may keep each as
# rows of its own (written once) instead of rewriting them with the rest of the state.
APPEND_ONLY_FIELDS = ("messages", "traces", "events")

# The audit events the engine writes (`SessionState.audit`) and the keys their `data` may hold.
# Every value is an id (party, claim), a field name, a count, a boolean or a fixed code: never
# text the caller or the model produced, no identifier values, no amounts. The trail may be kept
# much longer than the conversation, so a new key or code needs the same care
# (tests/test_audit.py checks every value against the codes it knows).
AUDIT_EVENTS: dict[str, tuple[str, ...]] = {
    "conversation_started": ("consent_scenario", "phase"),
    "tool_called": ("tool", "actor", "case_id", "case_ids", "topic", "party_id", "refused", "reason"),
    "identity_checked": (
        "matched_fields", "mismatched_fields", "matched_count", "policy_number_status", "candidate_party_id",
    ),
    "verification_strike": ("fields", "failed_attempts", "max_failed_attempts"),
    "verification_refusal": ("refusal", "refusal_count", "max_persuasion_attempts"),
    "identity_verified": (
        "party_id", "caller_role", "matched_fields", "matched_count", "required_matches",
        "identifier_values_cleared",  # identity_claims and seen_mismatches emptied at this point
    ),
    "role_change_ignored": ("kept_role",),
    "consent_requested": ("party_id", "channel"),
    "consent_result": ("party_id", "result", "polls"),
    "phase_changed": ("from", "to", "reason"),
    "claim_selected": ("case_id", "path"),
    "claim_choice_rejected": ("case_id", "reason"),
    "out_of_scope": ("scope", "consecutive", "total", "limit"),
    "human_offered": ("reason",),
    "escalated": ("reason", "verified", "phase"),
    "turn_limit_reached": ("limit",),
    "email_offered": ("remembered_preference",),
    "email_other_address_declined": ("reasks", "max_reasks"),
    "email_decision": ("decision", "defaulted"),
    "email_queued": ("recipient", "case_ids", "summary_source"),
    "guard_blocked": ("target", "rules", "outcome"),
    "model_refused": ("call", "outcome"),
    "conversation_closed": ("status", "reason", "verified", "phase"),
    "conversation_reopened": ("reason", "phase"),
}


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
    # What the caller supplied, as typed; emptied once verified (policy._forget_identifier_values).
    identity_claims: dict[str, str] = field(default_factory=dict)
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
    # Wrong values already counted as a strike, as "field:<keyed digest of the normalised value>"
    # (policy._mismatch_key), so a repeat can be told from a new value without keeping the value.
    # Records written before this held "field=<normalised value>"; those are still recognised.
    # Emptied once verified, like identity_claims.
    seen_mismatches: list[str] = field(default_factory=list)
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
    # The audit trail: what the harness decided or did, in order. Never what was said: ids,
    # field names, counts and enums only (see `audit`). A store may keep it longer than the
    # conversation itself.
    events: list[dict[str, Any]] = field(default_factory=list)
    # Fields written by a newer version of the code that this one does not know. They are
    # carried along and written back unchanged, so old and new processes can serve the same
    # conversation while a deployment rolls out.
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    # -- helpers -------------------------------------------------------------
    def audit(self, event: str, **data: Any) -> None:
        """Append one audit event. `data` must not hold identifier values, message text or
        amounts: only ids (party, claim), field names, counts and enums."""
        self.events.append(
            {"seq": len(self.events), "turn": self.turn, "at": round(time.time(), 3), "type": event, "data": data}
        )

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
        record: dict[str, Any] = {"format": RECORD_FORMAT, **self.unknown_fields}
        for f in fields(self):
            if f.name == "unknown_fields":
                continue
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
        was added still load. A field this version does not know (written by a newer one) is
        kept in `unknown_fields` and written back by `to_record`, so it is neither an error nor
        lost. A different `format` is an error: that marks a change old code must not guess at.
        """
        data = dict(record)
        fmt = data.pop("format", RECORD_FORMAT)
        if fmt != RECORD_FORMAT:
            raise ValueError(f"unsupported session record format {fmt!r}")
        known = {f.name for f in fields(cls)} - {"unknown_fields"}
        carried = {name: data.pop(name) for name in sorted(set(data) - known)}
        data["unknown_fields"] = carried
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
