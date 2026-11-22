"""Structured reading of one caller message.

The 'understand' model call fills this schema; the policy engine acts on it.
The model reports what was *said* — it has no way to mark anyone verified,
change phase or run a tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .identity import ALL_ID_FIELDS, PII_FIELDS, names_match

INTENTS = (
    "none",
    "status_inquiry",
    "denial_question",
    "document_submission",
    "next_steps",
    "payment_question",
    "general_claim_question",
)
CASE_TYPES = ("", "healthcare", "dental", "auto", "other")
CLAIM_STATUSES = ("", "denied", "open", "closed")
SCOPES = ("in_scope", "out_of_scope", "mixed")
EMOTIONS = ("neutral", "positive", "confused", "anxious", "frustrated", "angry", "distressed")
INTENSITIES = ("none", "mild", "moderate", "strong")
REFUSALS = ("none", "identity_info", "specific_field", "consent", "other")
ROLES = ("policyholder", "representative", "unknown")
YES_NO = ("none", "yes", "no")
EMAIL_CHOICES = ("none", "send", "skip")
HINT_KEYS = ("case_id", "case_type", "status", "time_reference", "description")


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _enum(values: tuple[str, ...], desc: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": desc}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


UNDERSTANDING_SCHEMA: dict[str, Any] = _obj(
    {
        "summary": _str("One short sentence describing what the caller said or wants."),
        "identity": _obj(
            {
                "full_name": _str("Policyholder's full name as stated in this message, else ''."),
                "dob": _str("Policyholder's date of birth as YYYY-MM-DD, else ''."),
                "phone": _str("Phone number as stated, else ''."),
                "email": _str("Email address as stated, else ''."),
                "id_last4": _str("Last four digits of SSN or national ID, else ''."),
                "policy_number": _str("Policy number as stated, else ''."),
            }
        ),
        "caller": _obj(
            {
                "role": _enum(ROLES, "Who the caller says they are in this message."),
                "representative_name": _str("Caller's own name if they are a representative, else ''."),
                "relationship": _str("Relationship to the policyholder (e.g. son), else ''."),
            }
        ),
        "claim_hints": _obj(
            {
                "case_id": _str("Claim number mentioned (format like CL-1234), else ''."),
                "case_type": _enum(CASE_TYPES, "Kind of claim mentioned, else ''."),
                "status": _enum(CLAIM_STATUSES, "Claim status as the caller describes it, else ''."),
                "time_reference": _str("When the claim was, in the caller's words, else ''."),
                "description": _str("Other specifics about the claim or need, else ''."),
            }
        ),
        "intent": _enum(INTENTS, "What the caller wants to do about a claim."),
        "scope": _enum(SCOPES, "Whether the message is within insurance claims support."),
        "out_of_scope_topic": _str("A few words naming the unrelated topic, else ''."),
        "emotion": _enum(EMOTIONS, "Dominant emotion in this message."),
        "emotion_intensity": _enum(INTENSITIES, "Strength of that emotion."),
        "refusal": _enum(REFUSALS, "What, if anything, the caller refuses."),
        "refused_fields": {
            "type": "array",
            "items": {"type": "string", "enum": list(PII_FIELDS)},
            "description": "Identifiers the caller declines to give.",
        },
        "asks_why": {"type": "boolean", "description": "Caller questions why a step is needed."},
        "requests_human": {
            "type": "boolean",
            "description": "Caller asks for, or accepts an offer of, a human representative.",
        },
        "wants_to_end": {"type": "boolean", "description": "Caller signals they are finished."},
        "wants_different_claim": {
            "type": "boolean",
            "description": "Caller asks about a different claim than the one under discussion.",
        },
        "yes_no": _enum(YES_NO, "Direct yes/no answer to the agent's last question."),
        "email_summary_choice": _enum(
            EMAIL_CHOICES,
            "Caller accepts or declines an email summary sent to the address on file.",
        ),
        "email_to_other_address": {
            "type": "boolean",
            "description": "Caller wants the summary sent to an address other than the one on file.",
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 3 other facts worth remembering for later steps.",
        },
    }
)

INTENSITY_LEVEL = {"none": 0, "mild": 1, "moderate": 2, "strong": 3}


@dataclass
class Understanding:
    summary: str = ""
    identity: dict[str, str] = field(default_factory=dict)  # only non-empty fields
    caller_role: str = "unknown"
    rep_name: str = ""
    rep_relationship: str = ""
    hints: dict[str, str] = field(default_factory=dict)  # only non-empty keys
    intent: str = "none"
    scope: str = "in_scope"
    out_of_scope_topic: str = ""
    emotion: str = "neutral"
    intensity: int = 0
    refusal: str = "none"
    refused_fields: list[str] = field(default_factory=list)
    asks_why: bool = False
    requests_human: bool = False
    wants_to_end: bool = False
    wants_different_claim: bool = False
    yes_no: str = "none"
    email_summary_choice: str = "none"
    email_other_address: bool = False
    notes: list[str] = field(default_factory=list)
    degraded: bool = False  # True when the reading is a safe default, not a model output

    @property
    def has_claim_reference(self) -> bool:
        return bool(self.hints) or self.intent != "none"

    def trace_view(self) -> dict[str, Any]:
        """What goes into the audit trace: identifier values are withheld."""
        return {
            "summary": self.summary,
            "identity_fields_extracted": sorted(self.identity),
            "caller_role": self.caller_role,
            "representative_named": bool(self.rep_name),
            "claim_hints": self.hints,
            "intent": self.intent,
            "scope": self.scope,
            "out_of_scope_topic": self.out_of_scope_topic,
            "emotion": f"{self.emotion} ({self.intensity}/3)",
            "refusal": self.refusal,
            "refused_fields": self.refused_fields,
            "asks_why": self.asks_why,
            "requests_human": self.requests_human,
            "wants_to_end": self.wants_to_end,
            "wants_different_claim": self.wants_different_claim,
            "yes_no": self.yes_no,
            "email_summary_choice": self.email_summary_choice,
            "email_to_other_address": self.email_other_address,
            "notes": self.notes,
            "degraded": self.degraded,
        }


def _pick(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in allowed else default


_MARKUP = re.compile(r"[<>`{}\[\]\\]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_CASE_ID = re.compile(r"[A-Z]{1,6}-?\d{1,12}")
_SELF_WORDS = {"self", "myself", "me", "policyholder", "the policyholder", "account holder", "owner"}


def _clean(value: Any, limit: int) -> str:
    """Free text from the model is caller-derived and ends up in prompts and the trace, so it
    is reduced to one short plain line: no markup characters, no line breaks, capped length."""
    text = _MARKUP.sub(" ", str(value or ""))
    text = _CONTROL.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def parse_understanding(raw: dict[str, Any] | None) -> Understanding:
    """Validate/normalise a model (or mock) output into an Understanding.

    Anything missing or malformed falls back to the neutral default, so a bad
    model output can never grant more than a careful reading would.
    """
    raw = raw if isinstance(raw, dict) else {}
    ident_raw = raw.get("identity") if isinstance(raw.get("identity"), dict) else {}
    identity = {f: _clean(ident_raw.get(f), 100) for f in ALL_ID_FIELDS}
    identity = {f: v for f, v in identity.items() if v}
    caller = raw.get("caller") if isinstance(raw.get("caller"), dict) else {}
    role = _pick(caller.get("role"), ROLES, "unknown")
    rep_name = _clean(caller.get("representative_name"), 80)
    relationship = _clean(caller.get("relationship"), 40)
    if role != "representative":
        # "Representative" fields that merely restate the policyholder are not a third party.
        if rep_name and names_match(rep_name, identity.get("full_name", "")):
            rep_name = ""
        if relationship.lower() in _SELF_WORDS:
            relationship = ""
    hints_raw = raw.get("claim_hints") if isinstance(raw.get("claim_hints"), dict) else {}
    hints: dict[str, str] = {}
    for key in HINT_KEYS:
        value = _clean(hints_raw.get(key), 160 if key == "description" else 60)
        if not value:
            continue
        if key == "case_type":
            value = _pick(value, CASE_TYPES, "")
        elif key == "status":
            value = _pick(value, CLAIM_STATUSES, "")
        elif key == "case_id":
            value = value.upper().replace(" ", "")
            if not _CASE_ID.fullmatch(value):
                value = ""
        if value:
            hints[key] = value
    refused = raw.get("refused_fields") if isinstance(raw.get("refused_fields"), list) else []
    notes = raw.get("notes") if isinstance(raw.get("notes"), list) else []
    return Understanding(
        summary=_clean(raw.get("summary"), 200),
        identity=identity,
        caller_role=role,
        rep_name=rep_name,
        rep_relationship=relationship,
        hints=hints,
        intent=_pick(raw.get("intent"), INTENTS, "none"),
        scope=_pick(raw.get("scope"), SCOPES, "in_scope"),
        out_of_scope_topic=_clean(raw.get("out_of_scope_topic"), 60),
        emotion=_pick(raw.get("emotion"), EMOTIONS, "neutral"),
        intensity=INTENSITY_LEVEL[_pick(raw.get("emotion_intensity"), INTENSITIES, "none")],
        refusal=_pick(raw.get("refusal"), REFUSALS, "none"),
        refused_fields=[f for f in refused if f in PII_FIELDS],
        asks_why=bool(raw.get("asks_why")),
        requests_human=bool(raw.get("requests_human")),
        wants_to_end=bool(raw.get("wants_to_end")),
        wants_different_claim=bool(raw.get("wants_different_claim")),
        yes_no=_pick(raw.get("yes_no"), YES_NO, "none"),
        # Agreeing to an email at some other address is not agreement to send it to the one
        # on file, so it never counts as "send".
        email_summary_choice=(
            "none"
            if raw.get("email_to_other_address")
            and _pick(raw.get("email_summary_choice"), EMAIL_CHOICES, "none") == "send"
            else _pick(raw.get("email_summary_choice"), EMAIL_CHOICES, "none")
        ),
        email_other_address=bool(raw.get("email_to_other_address")),
        notes=[n for n in (_clean(n, 160) for n in notes) if n][:3],
    )


RESOLUTION_SCHEMA: dict[str, Any] = _obj(
    {
        "case_id": _str("The single claim the caller means (must be one of the listed claims), or ''."),
        "candidate_case_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Claims that plausibly fit when it is ambiguous; empty if one was selected.",
        },
        "intent": _enum(INTENTS, "The workflow path that best matches the caller's need."),
        "rationale": _str("One sentence explaining the choice."),
    }
)

SUMMARY_SCHEMA: dict[str, Any] = _obj(
    {
        "subject": _str("Email subject line."),
        "discussed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-5 short bullets: what was discussed.",
        },
        "claim_status": _str("1-3 sentences: status/outcome of each claim discussed."),
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Follow-up items for the policyholder, with deadlines where the data has them.",
        },
    }
)
