"""Output guard: deterministic checks on model-written text before anyone sees it.

The primary protection is structural — before verification no record data is
in the model's context at all, so it has nothing to leak. This guard is the
tripwire behind that. It rejects text that

* contains record data the caller is not entitled to (other people's data,
  stored identifiers read back, claim data before verification),
* states that verification is complete while it is not,
* states dollar amounts or claim numbers that are not in the grounded data.

It is applied to chat replies and to the model-drafted email summary.
"""

from __future__ import annotations

import re
from typing import Any

from . import identity
from .data import Repository
from .state import SessionState

# Currency amounts however they are written: $1,450.00 / USD 1,450 / 1,450 dollars / 1,450.00
_AMOUNT = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)"
    r"|\bUSD\s?(\d[\d,]*(?:\.\d{1,2})?)"
    r"|(\d[\d,]*(?:\.\d{1,2})?)\s?(?:dollars?|USD)\b"
    r"|(?<![\w.,$-])(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?![\w%])",
    re.IGNORECASE,
)
_JSON_NUMBER = re.compile(r'"(\d+(?:\.\d{1,2})?)"')
_CASE_ID = re.compile(r"\bCL-\d+\b", re.IGNORECASE)
_DATE_LIKE = re.compile(
    r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"
    r"|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b"
    r"|\b[A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)? (?:of )?[A-Za-z]{3,9}\.?,? \d{4}\b"
)
_ID_CONTEXT = r"(?:ssn|social|national id|\bid\b|last (?:four|4)|ending|digits)\D{0,25}"
_VERIFIED_CLAIM = re.compile(
    r"you(?:'|’)?re (?:now |all set and |fully |already )?verified"
    r"|you are (?:now |fully |already )?verified"
    r"|(?:identity|you) (?:is|has been|have been) (?:now |successfully )?(?:verified|confirmed)"
    r"|verification (?:is |was )?(?:now )?(?:complete|successful)",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(
    r"\b(once|after|when|until|as soon as|before|if|unless|not|isn't|aren't|haven't|hasn't|can't|cannot)\b[^.!?]{0,40}$",
    re.IGNORECASE,
)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _dates_in(text: str) -> set[str]:
    found = set()
    for m in _DATE_LIKE.finditer(text):
        iso = identity.normalize_dob(m.group(0))
        if iso:
            found.add(iso)
    return found


class _CallerSaid:
    """What the caller typed, message by message (values they supplied may be echoed)."""

    def __init__(self, state: SessionState) -> None:
        texts = state.user_texts()
        self.lower = [t.lower() for t in texts]
        self.digits = [_digits(t) for t in texts]
        self.dates: set[str] = set()
        for t in texts:
            self.dates |= _dates_in(t)

    def text(self, token: str) -> bool:
        return any(token.lower() in t for t in self.lower)

    def digit_run(self, token: str) -> bool:
        return any(token in d for d in self.digits)

    def number(self, token: str) -> bool:
        pattern = re.compile(rf"(?<!\d){re.escape(token)}(?!\d)")
        return any(pattern.search(t) for t in self.lower)


def leak_violations(
    text: str, state: SessionState, repo: Repository, allow_echo: bool = True
) -> list[str]:
    """Record data in `text` that this caller is not entitled to see.

    Before verification: any policyholder's identifiers or any claim data.
    After verification: other policyholders' identifiers and claims, and the
    verified policyholder's own stored identifiers (never read back).
    With `allow_echo`, values the caller typed themselves may be repeated.
    """
    low = text.lower()
    text_digits = _digits(text)
    text_dates = _dates_in(text)
    said = _CallerSaid(state)
    party = state.party_id if state.verified else None
    violations: list[str] = []

    def flag(present: bool, echoed: bool, label: str) -> None:
        if present and not (allow_echo and echoed):
            violations.append(label)

    for holder in repo.policyholders:
        own = holder.party_id == party
        tag = "own stored identifier" if own else "another person's identifier"
        for email in (holder.email, *holder.email_aliases):
            flag(email.lower() in low, said.text(email), f"{tag}: email")
        for phone in (holder.phone, *holder.phone_aliases):
            number = identity.normalize_phone(phone) or ""
            flag(bool(number) and number in text_digits, said.digit_run(number), f"{tag}: phone")
        flag(holder.dob in text_dates, holder.dob in said.dates, f"{tag}: date of birth")
        # A bare 4-digit number proves nothing; only flag it where it is presented as ID digits.
        last4 = re.search(rf"(?i){_ID_CONTEXT}{re.escape(holder.id_last4)}(?!\d)", text)
        flag(bool(last4), said.number(holder.id_last4), f"{tag}: id last 4")
        if not own:
            flag(
                holder.policy_number.lower() in low,
                said.text(holder.policy_number),
                f"{tag}: policy number",
            )

    for claim in repo.claims:
        if party and claim.get("party_id") == party:
            continue
        label = "claim data before verification" if not party else "another person's claim data"
        # Claim numbers and denial reasons are specific enough to detect verbatim; the
        # one-line summaries ("Auto claim in progress") are too generic to match on.
        case_id = claim.get("case_id") or ""
        flag(bool(case_id) and case_id.lower() in low, said.text(case_id), f"{label}: case id")
        reason = claim.get("denial_reason") or ""
        flag(bool(reason) and reason.lower() in low, said.text(reason), f"{label}: denial reason")
    return violations


def premature_verification_claims(text: str) -> list[str]:
    """Statements that the caller is verified (for use while they are not).

    "Once you're verified, I can…" is fine; "You're verified." is not.
    """
    for m in _VERIFIED_CLAIM.finditer(text):
        if not _CONDITIONAL.search(text[: m.start()]):
            return [f"states that verification is complete: “{m.group(0)}”"]
    return []


def _amounts(text: str) -> set[float]:
    found: set[float] = set()
    for m in _AMOUNT.finditer(text):
        raw = next(g for g in m.groups() if g)
        try:
            found.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            continue
    return found


def grounded_amounts(state: SessionState, claims: list[dict[str, Any]]) -> set[float]:
    """Dollar figures the model may state: those in the claim records and tool results it was
    shown, plus the simple difference between two amounts on the same claim."""
    grounded_text = " ".join(state.grounding)
    allowed = _amounts(grounded_text)
    for m in _JSON_NUMBER.finditer(grounded_text):  # records hold amounts as "1450.00"
        allowed.add(round(float(m.group(1)), 2))
    for claim in claims:
        figures = []
        for key in ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee"):
            try:
                figures.append(round(float(claim.get(key)), 2))
            except (TypeError, ValueError):
                continue
        allowed |= set(figures)
        allowed |= {round(abs(a - b), 2) for a in figures for b in figures}
    return allowed


def grounding_violations(
    text: str, state: SessionState, claims: list[dict[str, Any]], owned_case_ids: set[str]
) -> list[str]:
    """Amounts and claim numbers in `text` that are not backed by grounded data.

    Before verification nothing is grounded, so any amount is a violation. An amount the
    caller mentioned is not grounded either: repeating it would read as confirmation.
    """
    violations: list[str] = []
    allowed = grounded_amounts(state, claims)
    for m in _AMOUNT.finditer(text):
        raw = next(g for g in m.groups() if g)
        try:
            value = round(float(raw.replace(",", "")), 2)
        except ValueError:
            continue
        if value not in allowed:
            violations.append(f"amount not in the claim data: {m.group(0).strip()}")

    said = _CallerSaid(state)
    for m in _CASE_ID.finditer(text):
        case_id = m.group(0).upper()
        if case_id not in owned_case_ids and not said.text(case_id):
            violations.append(f"claim number not on this account: {case_id}")
    return violations


def reply_violations(
    text: str,
    state: SessionState,
    repo: Repository,
    claims: list[dict[str, Any]],
    owned_case_ids: set[str],
) -> tuple[list[str], list[str]]:
    """(hard, soft) violations for a chat reply.

    Hard: protected data or a false claim of verification — the reply is replaced.
    Soft: ungrounded figures or claim numbers — one regeneration is allowed.
    """
    hard = leak_violations(text, state, repo)
    if not state.verified:
        hard += premature_verification_claims(text)
    return hard, grounding_violations(text, state, claims, owned_case_ids)


def summary_violations(
    fields: list[str],
    state: SessionState,
    repo: Repository,
    claims: list[dict[str, Any]],
    owned_case_ids: set[str],
) -> list[str]:
    """Violations in a model-drafted email summary. Stricter than chat: identifiers are
    never allowed in the email, even ones the caller typed during the conversation."""
    violations: list[str] = []
    for text in fields:
        violations += leak_violations(text, state, repo, allow_echo=False)
        violations += grounding_violations(text, state, claims, owned_case_ids)
    return violations


# The rules above by fixed name, for the audit trail: a violation text can hold the offending
# value (an amount, a claim number, the model's own words), so only these names are recorded.
# Leak rules name the kind of data too ("other_person_identifier.dob").
_LEAK_RULE_PREFIXES = (
    ("own stored identifier: ", "own_identifier"),
    ("another person's identifier: ", "other_person_identifier"),
    ("claim data before verification: ", "claim_data_before_verification"),
    ("another person's claim data: ", "other_person_claim_data"),
)
_OTHER_RULE_PREFIXES = (
    ("states that verification is complete: ", "premature_verification"),
    ("amount not in the claim data: ", "ungrounded_amount"),
    ("claim number not on this account: ", "ungrounded_claim_number"),
)
_LEAK_ITEMS = {
    "email": "email",
    "phone": "phone",
    "date of birth": "dob",
    "id last 4": "id_last4",
    "policy number": "policy_number",
    "case id": "case_id",
    "denial reason": "denial_reason",
}
UNNAMED_RULE = "unnamed_rule"  # a violation text this table does not know (a wording change)
RULE_NAMES: frozenset[str] = frozenset(
    {rule for _, rule in _LEAK_RULE_PREFIXES + _OTHER_RULE_PREFIXES}
    | {f"{rule}.{item}" for _, rule in _LEAK_RULE_PREFIXES for item in _LEAK_ITEMS.values()}
    | {UNNAMED_RULE}
)


def rule_names(violations: list[str]) -> list[str]:
    """The fixed names of the rules behind `violations` (sorted, each once)."""
    names = set()
    for text in violations:
        for prefix, rule in _LEAK_RULE_PREFIXES:
            if text.startswith(prefix):
                item = _LEAK_ITEMS.get(text[len(prefix):])
                names.add(f"{rule}.{item}" if item else rule)
                break
        else:
            names.add(next((rule for prefix, rule in _OTHER_RULE_PREFIXES if text.startswith(prefix)),
                           UNNAMED_RULE))
    return sorted(names)
