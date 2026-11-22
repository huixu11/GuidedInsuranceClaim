"""The deterministic SOP engine.

`step()` takes the structured reading of the caller's message and decides, in
plain code, everything the SOP is strict about: what is remembered, whether
identity is verified, which phase we are in, which tools run, when to stop and
escalate. Its output is a `Directive` — the move the reply must make plus the
facts the LLM is allowed to use while phrasing it.

The LLM is consulted from here only through two bounded, validated calls
(`backend.resolve` to pick one of the caller's own claims and a workflow path,
`backend.summarize` to draft the email summary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from . import guard, identity, sop
from .config import AGENT_NAME, COMPANY_NAME, Settings
from .data import Repository
from .identity import FIELD_SPOKEN, MATCHED, MISMATCHED, NOT_PROVIDED, PII_FIELDS
from .state import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_ESCALATED,
    MemoryItem,
    Phase,
    SessionState,
    Trace,
)
from .tools import Toolbox, ToolError
from .understanding import INTENTS, Understanding

# Directive actions ------------------------------------------------------------
REQUEST_IDENTITY = "REQUEST_IDENTITY"
REP_NOT_ON_FILE = "REP_NOT_ON_FILE"
CONSENT_REQUESTED = "CONSENT_REQUESTED"
CONSENT_PENDING = "CONSENT_PENDING"
CONSENT_TIMEOUT = "CONSENT_TIMEOUT"
CLOSE_UNVERIFIED = "CLOSE_UNVERIFIED"
CLARIFY_CASE = "CLARIFY_CASE"
NO_CLAIMS = "NO_CLAIMS"
ANSWER_CASE = "ANSWER_CASE"
OFFER_EMAIL = "OFFER_EMAIL"
EMAIL_SENT = "EMAIL_SENT"
EMAIL_SKIPPED = "EMAIL_SKIPPED"
CLOSED = "CLOSED"
ESCALATE = "ESCALATE"
ESCALATED_NOTICE = "ESCALATED_NOTICE"

ESCALATION_REASONS = {
    "caller_requested": "the caller asked for a human representative",
    "verification_failed": "identity could not be verified within the allowed attempts",
    "verification_refused": "the caller declined identity verification after it was explained",
    "repeated_out_of_scope": "the caller kept asking about topics outside claims support",
}


@dataclass
class Modifiers:
    """Cross-cutting instructions layered on top of the phase's main move."""

    empathy: tuple[str, int] | None = None  # (emotion, intensity 1-3)
    explain_why: bool = False
    decline_topic: str | None = None
    nothing_in_scope: bool = False
    offer_human: bool = False
    offer_human_reason: str | None = None
    ambiguous_yes: bool = False  # "yes" right after a human was offered next to another question

    def to_dict(self) -> dict[str, Any]:
        return {
            "ambiguous_yes": self.ambiguous_yes,
            "empathy": list(self.empathy) if self.empathy else None,
            "explain_why": self.explain_why,
            "decline_topic": self.decline_topic,
            "nothing_in_scope": self.nothing_in_scope,
            "offer_human": self.offer_human,
            "offer_human_reason": self.offer_human_reason,
        }


@dataclass
class Directive:
    action: str
    facts: dict[str, Any] = field(default_factory=dict)
    mods: Modifiers = field(default_factory=Modifiers)
    deterministic: bool = False  # reply is a fixed template; no model call


class PolicyBackend(Protocol):
    """The two bounded LLM decisions the engine may ask for."""

    def resolve(
        self, state: SessionState, claims: list[dict[str, Any]], settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def summarize(
        self, state: SessionState, records: list[dict[str, Any]], settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


@dataclass
class Ctx:
    state: SessionState
    u: Understanding
    tools: Toolbox
    backend: PolicyBackend
    trace: Trace
    settings: Settings
    repo: Repository
    mods: Modifiers = field(default_factory=Modifiers)
    entered: set[Phase] = field(default_factory=set)
    new_identity: set[str] = field(default_factory=set)
    new_hints: list[str] = field(default_factory=list)
    just_verified: bool = False
    just_resolved: bool = False
    ambiguous_yes: bool = False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def step(
    state: SessionState,
    u: Understanding,
    tools: Toolbox,
    backend: PolicyBackend,
    trace: Trace,
    settings: Settings,
    repo: Repository,
) -> Directive:
    ctx = Ctx(state, u, tools, backend, trace, settings, repo)

    if state.status == STATUS_ESCALATED:
        return Directive(ESCALATED_NOTICE, deterministic=True)

    human_was_offered = state.human_offer_pending
    _remember(ctx)  # always, whatever the phase
    state.human_offer_pending = False

    # A request for a person is always honoured; no persuasion.
    if u.requests_human:
        return _finish(ctx, _escalate(ctx, "caller_requested"))

    # The last reply offered a human representative next to whatever else it asked. A bare
    # "yes" does not say which was meant, so it is not acted on either way: the offer stays
    # open and the reply asks.
    if (
        human_was_offered
        and u.yes_no == "yes"
        and u.email_summary_choice == "none"
        and not ctx.new_identity
    ):
        ctx.ambiguous_yes = True
        ctx.mods.ambiguous_yes = True
        ctx.mods.offer_human = True
        ctx.mods.offer_human_reason = "the earlier offer is still open"
        trace.add("gate", "ambiguous yes after a human was offered — asking which was meant", None)

    stop = _track_guardrails(ctx)
    if stop is not None:
        return _finish(ctx, stop)

    directive: Directive | None = None
    for _ in range(8):  # phases can cascade within one turn (verify → resolve → process)
        if state.status == STATUS_COMPLETED:
            directive = _handle_completed(ctx)
        else:
            directive = _HANDLERS[state.phase](ctx)
        if directive is not None:
            break
    if directive is None:  # defensive: should be unreachable
        raise sop.SOPViolation("policy engine did not produce a directive")
    return _finish(ctx, directive)


def _finish(ctx: Ctx, directive: Directive) -> Directive:
    state = ctx.state
    if state.status != STATUS_ACTIVE:
        ctx.mods.offer_human = False  # nothing left to offer once closed or handed off
    directive.mods = ctx.mods
    state.human_offer_pending = ctx.mods.offer_human
    ctx.trace.add(
        "directive",
        directive.action,
        {"facts": _redact_facts(directive.facts), "modifiers": ctx.mods.to_dict()},
    )
    return directive


# ---------------------------------------------------------------------------
# Memory: capture useful information whenever it is said
# ---------------------------------------------------------------------------
def _remember(ctx: Ctx) -> None:
    s, u = ctx.state, ctx.u
    stored: dict[str, Any] = {}
    phase = s.phase.value

    # Naming a different claim number is a request to switch claims, whatever else was flagged.
    named = u.hints.get("case_id")
    if s.selected_case_id and named and named != s.selected_case_id:
        u.wants_different_claim = True

    if not s.verified:
        for f, value in u.identity.items():
            new = identity.normalize(f, value) or value.strip().lower()
            old_raw = s.identity_claims.get(f)
            old = (identity.normalize(f, old_raw) or old_raw.strip().lower()) if old_raw else None
            if new != old:
                s.identity_claims[f] = value
                ctx.new_identity.add(f)
        if ctx.new_identity:
            stored["identifiers"] = sorted(ctx.new_identity)
        # Any sign of a third party (stated role, a representative's name, a relationship)
        # puts the call on the representative path, and it stays there: later claiming to be
        # the policyholder must not switch off the consent requirement.
        rep_signal = u.caller_role == "representative" or bool(u.rep_name or u.rep_relationship)
        if rep_signal:
            if s.caller_role != "representative":
                s.caller_role = "representative"
                stored["caller_role"] = "representative"
        elif u.caller_role == "policyholder":
            if s.caller_role == "representative":
                ctx.trace.add(
                    "gate",
                    "caller earlier identified as a representative — role change ignored, "
                    "consent still required",
                    None,
                )
            elif s.caller_role != "policyholder":
                s.caller_role = "policyholder"
                stored["caller_role"] = "policyholder"
        if u.rep_name:
            s.rep_name = u.rep_name
        if u.rep_relationship:
            s.rep_relationship = u.rep_relationship
        for f in u.refused_fields:
            if f not in s.declined_fields:
                s.declined_fields.append(f)

    if u.wants_different_claim:
        # A new reference replaces what was said about the previous claim, including what
        # the caller wanted to do about it.
        s.hints = {}
        s.intent = None
    for key, value in u.hints.items():
        current = s.hints.get(key)
        if current is None or current.value != value:
            s.hints[key] = MemoryItem(value, s.turn, phase)
            ctx.new_hints.append(key)
    if ctx.new_hints:
        stored["claim_hints"] = {k: s.hints[k].value for k in ctx.new_hints}

    if u.intent != "none" and (s.intent is None or s.intent.value != u.intent):
        s.intent = MemoryItem(u.intent, s.turn, phase)
        stored["intent"] = u.intent

    known = {n.value.lower() for n in s.notes}
    for note in u.notes:
        if note.lower() not in known and len(s.notes) < 12:
            s.notes.append(MemoryItem(note, s.turn, phase))
            known.add(note.lower())
            stored.setdefault("notes", []).append(note)

    if u.email_summary_choice != "none" and s.phase != Phase.POST_PROCESS and not u.email_other_address:
        s.email_pref = MemoryItem(u.email_summary_choice, s.turn, phase)
        stored["email_summary_preference"] = u.email_summary_choice

    if stored:
        later = s.phase == Phase.VERIFY_ID and any(
            k in stored for k in ("claim_hints", "intent", "email_summary_preference")
        )
        ctx.trace.add(
            "memory",
            "remembered " + ", ".join(stored) + (" — kept for a later phase" if later else ""),
            stored,
        )


# ---------------------------------------------------------------------------
# Guardrails that apply in every phase
# ---------------------------------------------------------------------------
def _track_guardrails(ctx: Ctx) -> Directive | None:
    s, u, cfg, mods = ctx.state, ctx.u, ctx.settings, ctx.mods

    # Scope
    if u.scope == "out_of_scope":
        s.oos_consecutive += 1
        s.oos_total += 1
        mods.nothing_in_scope = True
    elif u.scope == "mixed":
        s.oos_total += 1
    else:
        s.oos_consecutive = 0
    if u.scope != "in_scope":
        mods.decline_topic = u.out_of_scope_topic or "that topic"
        limit = cfg.out_of_scope_limit
        if u.scope == "mixed":
            label = f"off-topic part of the message declined (total {s.oos_total})"
        else:
            label = (
                f"off-topic message declined ({s.oos_consecutive} in a row, limit {limit}; "
                f"total {s.oos_total})"
            )
        ctx.trace.add("gate", label, {"topic": mods.decline_topic})
        if s.oos_consecutive > limit or s.oos_total > 2 * limit:
            return _escalate(ctx, "repeated_out_of_scope")
        if s.oos_consecutive == limit or s.oos_total == 2 * limit:
            mods.offer_human = True
            mods.offer_human_reason = "repeated questions outside claims support"

    # Emotion
    s.emotion_label, s.emotion_intensity = u.emotion, u.intensity
    elevated = u.emotion in {"frustrated", "angry", "distressed"} and u.intensity >= 2
    s.elevated_streak = s.elevated_streak + 1 if elevated else 0
    if u.emotion not in {"neutral", "positive"} and u.intensity >= 1:
        mods.empathy = (u.emotion, u.intensity)
    if s.elevated_streak >= cfg.elevated_emotion_offer_streak:
        mods.offer_human = True
        mods.offer_human_reason = "the caller has been upset for several turns"
    if u.asks_why:
        mods.explain_why = True
    return None


# ---------------------------------------------------------------------------
# Transitions and escalation
# ---------------------------------------------------------------------------
def _transition(ctx: Ctx, dst: Phase, reason: str) -> None:
    s = ctx.state
    src = s.phase
    sop.check_transition(s, dst, ctx.tools.owned_case_ids())  # raises SOPViolation if illegal
    if dst == Phase.RESOLVE_INTENT:  # (re)entering claim selection: nothing is selected
        s.selected_case_id = None
        s.selected_case_brief = None
        s.case_intent = None
    s.phase = dst
    s.phase_history.append({"turn": s.turn, "from": src.value, "to": dst.value, "reason": reason})
    ctx.entered.add(dst)
    ctx.trace.add("transition", f"{src.value} → {dst.value} ({reason})", None)


def build_handoff_summary(s: SessionState, reason: str, settings: Settings) -> str:
    matched = [FIELD_SPOKEN[f] for f in PII_FIELDS if s.field_status.get(f) == MATCHED]
    if s.verified:
        ident = "identity VERIFIED"
    else:
        ident = (
            f"identity NOT verified — {len(matched)} of {settings.required_id_matches} "
            f"identifiers matched ({', '.join(matched) or 'none'}); failed attempts: "
            f"{s.failed_attempts}"
        )
    lines = [
        "Handoff to human representative",
        f"Reason: {ESCALATION_REASONS.get(reason, reason)}",
        f"Phase at handoff: {s.phase.value} ({ident})",
        f"Caller role: {s.caller_role}"
        + (f" — {s.rep_name} ({s.rep_relationship or 'relationship not stated'})" if s.rep_name else ""),
    ]
    if s.consent_required:
        lines.append(f"Policyholder consent: {s.consent_status}")
    lines.append(f"Caller's stated need: {s.hint_summary() or 'not stated'}")
    if s.verified and s.selected_case_id:
        lines.append(f"Claim under discussion: {s.selected_case_id} (path: {s.case_intent})")
    lines.append(
        f"Caller state: {s.emotion_label} ({s.emotion_intensity}/3); verification refusals: "
        f"{s.refusal_count}; out-of-scope requests: {s.oos_total}"
    )
    for note in s.notes[:5]:
        lines.append(f"Note: {note.value}")
    if not s.verified:
        lines.append("Action: complete identity verification before sharing any claim details.")
    return "\n".join(lines)


def _escalate(ctx: Ctx, reason: str) -> Directive:
    s = ctx.state
    s.status = STATUS_ESCALATED
    s.human_offer_pending = False
    s.escalation = {
        "reason": reason,
        "turn": s.turn,
        "handoff_summary": build_handoff_summary(s, reason, ctx.settings),
    }
    ctx.trace.add("escalation", f"escalated to a human: {ESCALATION_REASONS.get(reason, reason)}", None)
    return Directive(
        ESCALATE,
        {
            "reason": reason,
            "reason_text": ESCALATION_REASONS.get(reason, reason),
            "verified": s.verified,
        },
    )


# ---------------------------------------------------------------------------
# VERIFY_ID — strict
# ---------------------------------------------------------------------------
def _handle_verify(ctx: Ctx) -> Directive | None:
    s, u, cfg, tools, mods = ctx.state, ctx.u, ctx.settings, ctx.tools, ctx.mods

    if u.wants_to_end and not ctx.new_identity:
        s.status = STATUS_COMPLETED
        ctx.trace.add("gate", "caller ended the conversation before verification", None)
        return Directive(CLOSE_UNVERIFIED)

    previously_matched = {f for f in PII_FIELDS if s.field_status.get(f) == MATCHED}
    result = tools.verify_identity()
    s.field_status = dict(result.field_status)
    s.policy_status = result.policy_status
    s.candidate_party_id = result.party_id

    # Strike accounting: one strike per turn that introduces a new non-matching value.
    new_mismatch: list[str] = []
    for f in result.mismatched:
        raw = s.identity_claims.get(f, "")
        key = f"{f}={identity.normalize(f, raw) or raw.strip().lower()}"
        if key not in s.seen_mismatches:
            s.seen_mismatches.append(key)
            new_mismatch.append(f)
    if new_mismatch:
        s.failed_attempts += 1
        ctx.trace.add(
            "gate",
            f"identifier mismatch — failed attempt {s.failed_attempts}/{cfg.max_failed_attempts}",
            {"mismatched": result.mismatched},
        )
        if s.failed_attempts >= cfg.max_failed_attempts:
            return _escalate(ctx, "verification_failed")

    # Refusal ladder: explain → explain + offer a human → stop persuading.
    # A message that grumbles but also moves verification forward is cooperation, not refusal.
    # "Forward" means a newly matched identifier or a counted strike — so every refusing turn
    # uses up one of three bounded budgets and the conversation cannot stall indefinitely.
    progressed = bool(set(result.matched) - previously_matched) or bool(new_mismatch)
    if u.refusal in {"identity_info", "consent"} and not progressed:
        s.refusal_count += 1
        if s.refusal_count > cfg.max_persuasion_attempts:
            ctx.trace.add(
                "gate",
                f"caller still refuses after {cfg.max_persuasion_attempts} persuasion attempts — "
                "stop persuading",
                None,
            )
            return _escalate(ctx, "verification_refused")
        ctx.trace.add(
            "gate",
            f"caller resisted verification — persuasion attempt {s.refusal_count} of "
            f"{cfg.max_persuasion_attempts}",
            None,
        )
        mods.explain_why = True
        if s.refusal_count >= cfg.max_persuasion_attempts:
            mods.offer_human = True
            mods.offer_human_reason = "the caller is reluctant to verify"

    matched_count = result.matched_count
    is_rep = s.caller_role == "representative"

    # The SOP's rule, as written: identity is verified once at least three of the five
    # identifiers match one record. (Another supplied identifier that does not match is a
    # counted strike above, but does not undo three that do.)
    if matched_count >= cfg.required_id_matches and result.party_id:
        if is_rep:
            if not s.rep_name:
                return Directive(REQUEST_IDENTITY, _identity_facts(ctx, result, new_mismatch, need_rep_name=True))
            s.consent_required = True
            rep_key = identity.normalize("full_name", s.rep_name) or ""
            if (s.consent_party_id, s.consent_rep) != (result.party_id, rep_key):
                # Authorisation and consent belong to one (policyholder, representative) pair.
                # If either changes, nothing obtained for the earlier pair carries over.
                _bind_consent(s, result.party_id, rep_key)
                s.rep_on_file = None
            if s.rep_on_file is None:
                s.rep_on_file = bool(tools.lookup_representative(result.party_id)["on_file"])
            if not s.rep_on_file:
                mods.offer_human = True
                mods.offer_human_reason = "representative is not authorized on file"
                ctx.trace.add("gate", "representative not on file — cannot proceed without the policyholder", None)
                return Directive(REP_NOT_ON_FILE, {"rep_name": s.rep_name})
            if s.consent_status in {"not_required", "not_requested"}:
                s.consent_status = tools.request_consent()
                if s.consent_status == "pending":
                    return Directive(CONSENT_REQUESTED, {"rep_first_name": _first(s.rep_name)})
            elif s.consent_status == "pending":
                s.consent_status = tools.check_consent_status()
            if s.consent_status == "timed_out":
                mods.offer_human = True
                mods.offer_human_reason = "policyholder consent was not received in time"
                ctx.trace.add("gate", "consent timed out — claim details stay locked", None)
                return Directive(CONSENT_TIMEOUT, {})
            if s.consent_status != "approved":
                return Directive(CONSENT_PENDING, {"polls": s.consent_polls})
            ctx.trace.add("gate", "policyholder consent approved", None)

        s.verified = True
        s.party_id = result.party_id
        ctx.just_verified = True
        ctx.trace.add(
            "gate",
            f"identity verified — {matched_count} identifiers matched one record"
            + (" + policyholder consent" if is_rep else ""),
            {"matched": result.matched},
        )
        _transition(ctx, Phase.RESOLVE_INTENT, "identity verified")
        return None

    return Directive(REQUEST_IDENTITY, _identity_facts(ctx, result, new_mismatch))


def _bind_consent(s: SessionState, party_id: str, rep_key: str) -> None:
    """Point the consent state at another (policyholder, representative) pair, keeping each
    pair's outcome: an approval is never inherited and a timed-out request is not restarted."""
    if s.consent_party_id is not None:
        s.consent_ledger[f"{s.consent_party_id}|{s.consent_rep or ''}"] = {
            "status": s.consent_status,
            "polls": s.consent_polls,
        }
    saved = s.consent_ledger.get(f"{party_id}|{rep_key}")
    s.consent_party_id, s.consent_rep = party_id, rep_key
    s.consent_status = saved["status"] if saved else "not_requested"
    s.consent_polls = saved["polls"] if saved else 0


def _identity_facts(
    ctx: Ctx,
    result: identity.VerificationResult,
    new_mismatch: list[str],
    need_rep_name: bool = False,
) -> dict[str, Any]:
    s, cfg = ctx.state, ctx.settings
    still_open = [f for f in result.missing if f not in s.declined_fields]
    reachable = result.matched_count + len(still_open) + len(result.mismatched)
    can_reach = reachable >= cfg.required_id_matches
    if not can_reach:
        ctx.mods.offer_human = True
        ctx.mods.offer_human_reason = "not enough identifiers left to complete verification"
    noted = None
    if ctx.new_hints or (ctx.state.intent and ctx.state.intent.captured_turn == s.turn):
        noted = s.hint_summary() or None
    return {
        "matched": [FIELD_SPOKEN[f] for f in result.matched],
        "mismatched": [FIELD_SPOKEN[f] for f in result.mismatched],
        "new_mismatch": [FIELD_SPOKEN[f] for f in new_mismatch],
        "options": [FIELD_SPOKEN[f] for f in still_open],
        "declined": [FIELD_SPOKEN[f] for f in s.declined_fields],
        "matched_count": result.matched_count,
        "required": cfg.required_id_matches,
        "remaining_needed": max(0, cfg.required_id_matches - result.matched_count),
        "attempts_left": max(0, cfg.max_failed_attempts - s.failed_attempts),
        "nothing_provided": not any(s.identity_claims.get(f) for f in PII_FIELDS),
        "provided_this_turn": [FIELD_SPOKEN[f] for f in sorted(ctx.new_identity)],
        "policy_number_status": result.policy_status,
        "is_representative": s.caller_role == "representative",
        "need_rep_name": need_rep_name,
        "refusal_stage": s.refusal_count,
        "can_still_verify": can_reach,
        "noted_for_later": noted,
    }


# ---------------------------------------------------------------------------
# RESOLVE_INTENT — guided
# ---------------------------------------------------------------------------
def _handle_resolve(ctx: Ctx) -> Directive | None:
    s, u, cfg, tools = ctx.state, ctx.u, ctx.settings, ctx.tools
    entered_now = Phase.RESOLVE_INTENT in ctx.entered
    claims = tools.list_claims()

    if u.wants_to_end and not entered_now:
        _transition(ctx, Phase.POST_PROCESS, "caller has nothing further")
        return None

    base = {
        "just_verified": ctx.just_verified,
        "caller": _caller_facts(ctx),
        "claims": claims,
    }
    if not claims:
        return Directive(NO_CLAIMS, base)

    said_something = bool(s.hints) or s.intent is not None
    if ctx.just_verified and not said_something and len(claims) > 1:
        # Nothing to interpret yet — ask, without spending a model call.
        s.candidates = []
        return Directive(CLARIFY_CASE, {**base, "candidates": [], "remembered": None})
    if ctx.mods.nothing_in_scope and not entered_now:
        # An off-topic message says nothing about which claim; keep the question open.
        return Directive(
            CLARIFY_CASE, {**base, "candidates": list(s.candidates), "remembered": _remembered(ctx)}
        )

    raw, meta = ctx.backend.resolve(s, claims, cfg)
    owned = {c["case_id"] for c in claims}
    case_id = str(raw.get("case_id") or "").strip().upper()
    intent = str(raw.get("intent") or "none")
    rejected = None
    if case_id and case_id not in owned:
        rejected, case_id = case_id, ""  # the model may only pick one of the caller's own claims
    candidates = [
        c for c in (str(x).strip().upper() for x in raw.get("candidate_case_ids") or []) if c in owned
    ]
    if intent not in INTENTS or intent == "none":
        intent = s.intent.value if s.intent else "general_claim_question"
    ctx.trace.add(
        "decision",
        "resolver → "
        + (f"claim {case_id}, path {intent}" if case_id else f"needs clarification ({len(candidates)} candidates)"),
        {
            "selected": case_id or None,
            "candidates": candidates,
            "intent": intent,
            "rationale": raw.get("rationale"),
            "rejected_out_of_bounds_choice": rejected,
            "used_remembered_hints": s.hint_summary() or None,
            **({"llm": meta} if meta else {}),
        },
    )

    if case_id:
        chosen = next(c for c in claims if c["case_id"] == case_id)
        s.selected_case_id = case_id
        s.selected_case_brief = (
            f"{case_id} ({chosen.get('case_type')} claim, filed {chosen.get('created_at')}, "
            f"{chosen.get('status')})"
        )
        s.case_intent = intent
        s.candidates = []
        s.clarify_attempts = 0
        ctx.just_resolved = True
        _transition(ctx, Phase.PROCESS_CASE, f"claim {case_id} selected")
        return None

    s.candidates = candidates
    s.clarify_attempts += 1
    if s.clarify_attempts > cfg.max_clarify_attempts:
        ctx.mods.offer_human = True
        ctx.mods.offer_human_reason = "could not determine which claim the caller means"
    return Directive(
        CLARIFY_CASE,
        {**base, "candidates": candidates, "remembered": _remembered(ctx)},
    )


# ---------------------------------------------------------------------------
# PROCESS_CASE — open (grounded)
# ---------------------------------------------------------------------------
def _handle_process(ctx: Ctx) -> Directive | None:
    s, u, tools = ctx.state, ctx.u, ctx.tools
    entered_now = Phase.PROCESS_CASE in ctx.entered

    if not entered_now:
        if u.wants_different_claim:
            s.selected_case_id = None
            s.case_intent = None
            _transition(ctx, Phase.RESOLVE_INTENT, "caller asked about a different claim")
            return None
        if u.wants_to_end:
            _transition(ctx, Phase.POST_PROCESS, "caller has no further questions")
            return None
        if u.intent != "none":
            s.case_intent = u.intent

    record = tools.get_claim_details(s.selected_case_id or "")
    if record["case_id"] not in s.cases_discussed:
        s.cases_discussed.append(record["case_id"])
    schema = ctx.repo.claim_schema.get("field_descriptions", {})
    return Directive(
        ANSWER_CASE,
        {
            "just_verified": ctx.just_verified,
            "just_resolved": ctx.just_resolved,
            "remembered": _remembered(ctx) if ctx.just_resolved else None,
            "caller": _caller_facts(ctx),
            "claim": record,
            "field_definitions": {k: v.get("description", "") for k, v in schema.items()},
            "path": s.case_intent or "general_claim_question",
            "path_description": sop.WORKFLOW_PATHS.get(s.case_intent or "", ""),
            "other_claims_on_file": max(0, (s.claims_on_file or 1) - 1),
        },
    )


# ---------------------------------------------------------------------------
# POST_PROCESS — guided
# ---------------------------------------------------------------------------
def _handle_post(ctx: Ctx) -> Directive | None:
    s, u, cfg = ctx.state, ctx.u, ctx.settings
    entered_now = Phase.POST_PROCESS in ctx.entered
    holder = ctx.repo.policyholder(s.party_id)
    email_facts = {
        "masked_email": identity.mask_email(holder.email if holder else None),
        "caller": _caller_facts(ctx),
        "cases_discussed": list(s.cases_discussed),
    }
    choice = u.email_summary_choice
    if u.email_other_address:
        # They want it somewhere else. The summary only ever goes to the address on file, and
        # agreeing to another address is not consent to that one - so say so and ask again.
        s.email_offered = True
        s.email_reasks += 1
        ctx.trace.add("gate", "summary requested to a different address — not sent; asking again", None)
        if s.email_reasks >= cfg.max_email_reasks:
            return _skip_email(ctx, email_facts, defaulted=True)
        return Directive(
            OFFER_EMAIL,
            {**email_facts, "remembered_preference": None, "reask": True, "other_address_requested": True},
        )

    if entered_now or not s.email_offered:
        s.email_offered = True
        s.email_reasks = 0
        if choice == "send":  # e.g. "that's all — and yes, email me the summary"
            return _send_email(ctx, email_facts)
        if choice == "skip":
            return _skip_email(ctx, email_facts)
        # The offer is always made here. If the caller already said what they would like
        # earlier in the call, that is remembered and mentioned, and they still choose.
        pref = s.email_pref.value if s.email_pref else None
        return Directive(OFFER_EMAIL, {**email_facts, "remembered_preference": pref, "reask": False})

    if choice == "none" and u.yes_no in {"yes", "no"} and not ctx.ambiguous_yes:
        choice = "send" if u.yes_no == "yes" else "skip"
    if choice == "send":
        return _send_email(ctx, email_facts)
    if choice == "skip":
        return _skip_email(ctx, email_facts)

    if u.wants_to_end:
        # A goodbye without a yes is not consent: nothing is sent, and we don't nag.
        return _skip_email(ctx, email_facts, defaulted=True)

    # Not an answer to the offer. A new claim question re-opens the earlier phases.
    if u.wants_different_claim or u.hints:
        s.email_offered = False
        s.selected_case_id = None
        s.case_intent = None
        _transition(ctx, Phase.RESOLVE_INTENT, "caller raised another claim")
        return None
    if u.intent != "none":
        s.email_offered = False
        if s.selected_case_id:
            s.case_intent = u.intent
            _transition(ctx, Phase.PROCESS_CASE, "caller has a follow-up question")
        else:
            _transition(ctx, Phase.RESOLVE_INTENT, "caller has a claim question")
        return None

    s.email_reasks += 1
    if s.email_reasks >= cfg.max_email_reasks:
        # Never send without an explicit yes.
        return _skip_email(ctx, email_facts, defaulted=True)
    return Directive(OFFER_EMAIL, {**email_facts, "remembered_preference": None, "reask": True})


def _send_email(ctx: Ctx, facts: dict[str, Any]) -> Directive:
    s = ctx.state
    s.email_decision = "send"
    ctx.trace.add("gate", "caller agreed to the email summary", None)
    records = []
    for case_id in s.cases_discussed:
        try:
            records.append(ctx.tools.get_claim_details(case_id))
        except ToolError:
            continue
    fallback = deterministic_summary(records)
    try:
        raw, meta = ctx.backend.summarize(s, records, ctx.settings)
    except Exception as exc:  # noqa: BLE001 - the email must not depend on the drafting call
        raw, meta = {}, {}
        ctx.trace.add("error", f"summary drafting failed ({type(exc).__name__}) — built from records", None)
    summary = _clean_summary(raw, fallback)
    if summary is not fallback:
        # The draft is model-written, so it goes through the same guard as a chat reply
        # (stricter: no identifiers at all) before it can leave as an email.
        problems = guard.summary_violations(
            [summary["subject"], *summary["discussed"], summary["claim_status"], *summary["next_steps"]],
            s,
            ctx.repo,
            records,
            ctx.tools.owned_case_ids(),
        )
        if problems:
            s.guard_blocks += 1
            ctx.trace.add("guard", "drafted summary rejected — email built from the records instead", problems)
            summary = fallback
    holder = ctx.repo.policyholder(s.party_id)
    body = render_email_body(summary, holder.first_name if holder else "", ctx.settings)
    if meta:
        ctx.trace.add("llm", "summary drafted", meta)
    try:
        sent = ctx.tools.send_summary_email(summary["subject"], body)
    except ToolError as exc:
        s.email_decision = None
        ctx.trace.add("error", f"email not sent: {exc}", None)
        s.status = STATUS_COMPLETED
        return Directive(EMAIL_SKIPPED, {**facts, "send_failed": True})
    s.status = STATUS_COMPLETED
    return Directive(
        EMAIL_SENT,
        {
            **facts,
            "sent_to": sent["to"],
            "subject": summary["subject"],
            "summary": summary,
            # For the template wording: taken from the records, never from model text.
            "next_step_from_records": (fallback["next_steps"] or [None])[0] if records else None,
        },
    )


def _skip_email(ctx: Ctx, facts: dict[str, Any], defaulted: bool = False) -> Directive:
    s = ctx.state
    s.email_decision = "skip"
    s.status = STATUS_COMPLETED
    ctx.trace.add(
        "gate",
        "email summary skipped" + (" (no clear answer — default is not to send)" if defaulted else ""),
        None,
    )
    return Directive(EMAIL_SKIPPED, {**facts, "defaulted": defaulted})


def _clean_summary(raw: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    """Shape a drafted summary; returns `fallback` itself when there is no usable draft."""
    if not isinstance(raw, dict) or not raw:
        return fallback

    def line(value: Any, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    def bullets(key: str) -> list[str]:
        items = raw.get(key)
        if not isinstance(items, list):
            return []
        return [b for b in (line(i, 400) for i in items) if b][:6]

    return {
        "subject": line(raw.get("subject"), 150) or fallback["subject"],
        "discussed": bullets("discussed") or fallback["discussed"],
        "claim_status": line(raw.get("claim_status"), 800) or fallback["claim_status"],
        "next_steps": bullets("next_steps") or fallback["next_steps"],
    }


def deterministic_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary built straight from the claim records (used in mock mode and as a fallback)."""
    if not records:
        return {
            "subject": f"Summary of your conversation with {COMPANY_NAME} claims support",
            "discussed": ["Your identity was verified and we reviewed your account."],
            "claim_status": "No specific claim was reviewed in this conversation.",
            "next_steps": ["Contact claims support again whenever you need help with a claim."],
        }
    discussed, status, steps = [], [], []
    for r in records:
        discussed.append(
            f"Claim {r['case_id']} ({r.get('case_type')}, filed {fmt_date(r.get('created_at'))}): "
            f"{r.get('summary')}"
        )
        line = f"Claim {r['case_id']} is {r.get('status')}."
        if r.get("denial_reason"):
            line += f" It was denied because {r['denial_reason']}."
        status.append(line)
        docs = r.get("documents_needed") or []
        if docs:
            steps.append(f"Submit for claim {r['case_id']}: {', '.join(docs)}.")
        if r.get("appeal_deadline"):
            steps.append(f"Appeal deadline for claim {r['case_id']}: {fmt_date(r['appeal_deadline'])}.")
    ids = ", ".join(r["case_id"] for r in records)
    return {
        "subject": f"Summary of your {COMPANY_NAME} claims conversation ({ids})",
        "discussed": discussed,
        "claim_status": " ".join(status),
        "next_steps": steps or ["No action is needed from you at this time."],
    }


def render_email_body(summary: dict[str, Any], first_name: str, settings: Settings) -> str:
    lines = [
        f"Hello {first_name}," if first_name else "Hello,",
        "",
        f"Thank you for contacting {COMPANY_NAME} claims support on "
        f"{fmt_date(settings.demo_today.isoformat())}. Here is a summary of our conversation.",
        "",
        "What we discussed",
        *[f"- {b}" for b in summary["discussed"]],
        "",
        "Claim status",
        summary["claim_status"],
        "",
        "Next steps",
        *[f"- {b}" for b in summary["next_steps"]],
        "",
        "If anything here looks wrong, or you have more questions, contact claims support "
        "and mention this summary.",
        "",
        f"{AGENT_NAME}, {COMPANY_NAME} virtual claims assistant",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Completed conversations can be re-opened by a new in-scope request
# ---------------------------------------------------------------------------
def _handle_completed(ctx: Ctx) -> Directive | None:
    s, u = ctx.state, ctx.u
    if not s.verified:
        if ctx.new_identity or u.has_claim_reference:
            s.status = STATUS_ACTIVE
            return None  # back into VERIFY_ID
        return Directive(CLOSED, {"verified": False})
    if (
        s.phase == Phase.POST_PROCESS
        and u.email_summary_choice == "send"
        and not u.email_other_address
        and s.email_decision != "send"
    ):
        # Changed their mind after skipping: an explicit request for the summary is honoured.
        s.status = STATUS_ACTIVE
        holder = ctx.repo.policyholder(s.party_id)
        return _send_email(
            ctx,
            {
                "masked_email": identity.mask_email(holder.email if holder else None),
                "caller": _caller_facts(ctx),
                "cases_discussed": list(s.cases_discussed),
            },
        )
    if s.phase == Phase.POST_PROCESS and (u.wants_different_claim or u.has_claim_reference):
        s.status = STATUS_ACTIVE
        s.email_offered = False
        s.email_decision = None
        s.email_reasks = 0
        if s.selected_case_id and not u.wants_different_claim and not u.hints:
            if u.intent != "none":
                s.case_intent = u.intent
            _transition(ctx, Phase.PROCESS_CASE, "caller came back with a follow-up")
        else:
            s.selected_case_id = None
            s.case_intent = None
            _transition(ctx, Phase.RESOLVE_INTENT, "caller came back about another claim")
        return None
    return Directive(CLOSED, {"verified": True, "caller": _caller_facts(ctx)})


_HANDLERS = {
    Phase.VERIFY_ID: _handle_verify,
    Phase.RESOLVE_INTENT: _handle_resolve,
    Phase.PROCESS_CASE: _handle_process,
    Phase.POST_PROCESS: _handle_post,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _first(name: str | None) -> str:
    return name.split()[0] if name and name.split() else ""


def _caller_facts(ctx: Ctx) -> dict[str, Any]:
    s = ctx.state
    holder = ctx.repo.policyholder(s.party_id)
    if holder is None:
        return {}
    if s.caller_role == "representative":
        return {
            "role": "representative",
            "address_as": _first(s.rep_name),
            "relationship": s.rep_relationship,
            "policyholder_name": holder.name,
        }
    return {"role": "policyholder", "address_as": holder.first_name}


def _remembered(ctx: Ctx) -> dict[str, Any] | None:
    """Hints the caller gave earlier (before this phase), so the reply can pick up from them."""
    s = ctx.state
    earlier = [
        item
        for item in [*s.hints.values(), *([s.intent] if s.intent else [])]
        if item.captured_phase == Phase.VERIFY_ID.value or item.captured_turn < s.turn
    ]
    if not earlier:
        return None
    return {
        "what_the_caller_said": s.hint_summary(),
        "said_during_verification": any(i.captured_phase == Phase.VERIFY_ID.value for i in earlier),
    }


def _redact_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Trace view of directive facts (claim record is already traced by the tool call)."""
    out = dict(facts)
    if "claim" in out:
        out["claim"] = f"<record {out['claim'].get('case_id')} — see get_claim_details>"
    if "claims" in out:
        out["claims"] = [c.get("case_id") for c in out["claims"]]
    out.pop("field_definitions", None)
    return out


def fmt_date(iso: str | None) -> str:
    """2026-01-12 → January 12, 2026."""
    from datetime import date

    try:
        d = date.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "Directive",
    "Modifiers",
    "step",
    "build_handoff_summary",
    "deterministic_summary",
    "render_email_body",
    "fmt_date",
    "fmt_money",
    "MISMATCHED",
    "NOT_PROVIDED",
]
