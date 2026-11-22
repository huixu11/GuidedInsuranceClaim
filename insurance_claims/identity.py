"""Deterministic identity matching.

The LLM only *extracts* what the caller said. Whether those values match a
policyholder record is decided here, in plain code, so the verification gate
cannot be talked around.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from .data import Policyholder, Repository

# The five identifiers the SOP accepts; any three must match.
PII_FIELDS: tuple[str, ...] = ("full_name", "dob", "phone", "email", "id_last4")
LOCATOR_FIELD = "policy_number"  # helps find the record; does not count toward the 3
ALL_ID_FIELDS: tuple[str, ...] = PII_FIELDS + (LOCATOR_FIELD,)

FIELD_LABELS = {
    "full_name": "Full name",
    "dob": "Date of birth",
    "phone": "Phone",
    "email": "Email",
    "id_last4": "SSN / ID last 4",
    "policy_number": "Policy number",
}

# How the agent should name each identifier when asking the caller for it.
FIELD_SPOKEN = {
    "full_name": "full name",
    "dob": "date of birth",
    "phone": "phone number on the policy",
    "email": "email address on the policy",
    "id_last4": "last four digits of the SSN or national ID",
    "policy_number": "policy number",
}

MATCHED = "matched"
MISMATCHED = "mismatched"
NOT_PROVIDED = "not_provided"

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for m in names
}


def _valid_date(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def normalize_dob(value: str | None) -> str | None:
    """Parse common date spellings into ISO YYYY-MM-DD (US month-first for numeric dates)."""
    if not value:
        return None
    s = value.strip().lower().replace(",", " ")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"\s+", " ", s)
    m = re.fullmatch(r"(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})", s)
    if m:
        return _valid_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 >= b:  # clearly day-first
            a, b = b, a
        return _valid_date(y, a, b)
    m = re.fullmatch(r"([a-z]+)\.? (\d{1,2}) (\d{4})", s)
    if m and m[1] in _MONTHS:
        return _valid_date(int(m[3]), _MONTHS[m[1]], int(m[2]))
    m = re.fullmatch(r"(\d{1,2}) (?:of )?([a-z]+)\.? (\d{4})", s)
    if m and m[2] in _MONTHS:
        return _valid_date(int(m[3]), _MONTHS[m[2]], int(m[1]))
    return None


def normalize_phone(value: str | None) -> str | None:
    """Digits of a phone number, without a trailing extension or a leading US country code.

    Anything that is not a plain 10-digit number is kept as-is and simply will not match.
    """
    if not value:
        return None
    text = re.sub(r"(?i)\b(?:ext|extension|x)\.?\s*\d+\s*$", "", value.strip())
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip(".,;:<>()[]").lower() or None


def normalize_last4(value: str | None) -> str | None:
    """Last four digits of an ID number.

    Accepts a single run of digits (the last four, or a longer ID number) or a full SSN
    written 123-45-6789. Anything else (several unrelated numbers in one value) is rejected
    rather than guessed at.
    """
    if not value:
        return None
    groups = re.findall(r"\d+", value)
    if len(groups) == 1 and len(groups[0]) >= 4:
        return groups[0][-4:]
    if [len(g) for g in groups] == [3, 2, 4]:
        return groups[2]
    return None


def normalize_policy(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^A-Z0-9]", "", value.upper()) or None


def _name_tokens(value: str) -> list[str]:
    """Lower-cased name parts with accents folded ("José García" → ["jose", "garcia"]).
    Letters of any script are kept."""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return [t for t in re.split(r"[\W_]+", text) if t]


def _runs_together(coarse: list[str], fine: list[str]) -> bool:
    """True if `coarse` is `fine` with some neighbouring parts written as one word
    ("yawen li" vs "ya wen li"). Parts are never split, so "mat ian" is not "ma tian"."""
    i = 0
    for token in coarse:
        built = ""
        while i < len(fine) and len(built) < len(token):
            built += fine[i]
            i += 1
        if built != token:
            return False
    return i == len(fine)


def names_match(given: str | None, on_file: str) -> bool:
    """Case/accent/order-insensitive comparison ("Chen, Margaret"), also accepting name parts
    written together ("Yawen Li" for "Ya Wen Li")."""
    if not given:
        return False
    a, b = _name_tokens(given), _name_tokens(on_file)
    if not a or not b:
        return False
    return sorted(a) == sorted(b) or _runs_together(a, b) or _runs_together(b, a)


NORMALIZERS = {
    # Sorted, so "Chen, Margaret" and "Margaret Chen" count as the same supplied value.
    "full_name": lambda v: " ".join(sorted(_name_tokens(v))) if v else None,
    "dob": normalize_dob,
    "phone": normalize_phone,
    "email": normalize_email,
    "id_last4": normalize_last4,
    "policy_number": normalize_policy,
}


def normalize(field_name: str, value: str | None) -> str | None:
    """Canonical form used to compare values and detect corrections."""
    if value is None or not str(value).strip():
        return None
    return NORMALIZERS[field_name](str(value))


def _field_matches(field_name: str, value: str, holder: Policyholder) -> bool:
    if field_name == "full_name":
        return any(names_match(value, n) for n in (holder.name, *holder.name_aliases))
    if field_name == "dob":
        return normalize_dob(value) == holder.dob
    if field_name == "phone":
        given = normalize_phone(value)
        return bool(given) and given in {
            normalize_phone(p) for p in (holder.phone, *holder.phone_aliases)
        }
    if field_name == "email":
        given = normalize_email(value)
        return bool(given) and given in {
            normalize_email(e) for e in (holder.email, *holder.email_aliases)
        }
    if field_name == "id_last4":
        return normalize_last4(value) == holder.id_last4
    if field_name == "policy_number":
        return normalize_policy(value) == normalize_policy(holder.policy_number)
    return False


@dataclass
class VerificationResult:
    party_id: str | None = None
    record_found: bool = False
    field_status: dict[str, str] = field(default_factory=dict)
    policy_status: str = NOT_PROVIDED

    @property
    def matched(self) -> list[str]:
        return [f for f in PII_FIELDS if self.field_status.get(f) == MATCHED]

    @property
    def mismatched(self) -> list[str]:
        return [f for f in PII_FIELDS if self.field_status.get(f) == MISMATCHED]

    @property
    def missing(self) -> list[str]:
        return [f for f in PII_FIELDS if self.field_status.get(f) == NOT_PROVIDED]

    @property
    def matched_count(self) -> int:
        return len(self.matched)

    def to_dict(self) -> dict:
        return {
            "record_found": self.record_found,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "not_provided": self.missing,
            "matched_count": self.matched_count,
            "policy_number": self.policy_status,
        }


def evaluate(provided: dict[str, str], repo: Repository) -> VerificationResult:
    """Compare what the caller supplied against policyholder records.

    The candidate record is the one that best fits the supplied identifiers
    (most PII matches, policy-number match as tie-breaker). Each supplied field
    is then reported as matched or mismatched against that single record, so
    identifiers from two different people can never add up to a pass.
    """
    given = {f: v for f, v in provided.items() if f in ALL_ID_FIELDS and v and str(v).strip()}
    result = VerificationResult(
        field_status={f: NOT_PROVIDED for f in PII_FIELDS}, policy_status=NOT_PROVIDED
    )
    if not given:
        return result

    best: tuple[tuple[int, int], Policyholder, dict[str, bool]] | None = None
    for holder in repo.policyholders:
        hits = {f: _field_matches(f, v, holder) for f, v in given.items()}
        pii_hits = sum(1 for f in PII_FIELDS if hits.get(f))
        score = (pii_hits, 1 if hits.get(LOCATOR_FIELD) else 0)
        if score == (0, 0):
            continue
        if best is None or score > best[0]:
            best = (score, holder, hits)

    if best is None:
        for f in PII_FIELDS:
            if f in given:
                result.field_status[f] = MISMATCHED
        if LOCATOR_FIELD in given:
            result.policy_status = MISMATCHED
        return result

    _, holder, hits = best
    result.party_id = holder.party_id
    result.record_found = True
    for f in PII_FIELDS:
        if f in given:
            result.field_status[f] = MATCHED if hits[f] else MISMATCHED
    if LOCATOR_FIELD in given:
        result.policy_status = MATCHED if hits[LOCATOR_FIELD] else MISMATCHED
    return result


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "the email address on file"
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}"
