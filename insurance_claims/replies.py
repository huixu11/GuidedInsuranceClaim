"""Deterministic reply templates.

For every directive the harness can issue there is a fixed, safe wording. It is
used (a) when the output guard rejects a model reply, (b) when the model call
fails or is refused, and (c) by the offline mock backend. This means the SOP
can always make its move, even with no model available.
"""

from __future__ import annotations

from typing import Any

from . import policy as P
from .config import AGENT_NAME, COMPANY_NAME, Settings
from .state import SessionState

ID_OPTIONS = (
    "your full name, date of birth, phone number, email address, or the last four digits "
    "of your SSN or national ID"
)

GREETING = (
    f"Thanks for contacting {COMPANY_NAME} claims support. I'm {AGENT_NAME}, a virtual assistant. "
    "Before we look at any claim I need to verify your identity. Could you give me any three of "
    f"these: {ID_OPTIONS}?"
)

ESCALATED_NOTICE = (
    "This conversation has been transferred to a human representative, so I can't continue it "
    "here. They have a summary of what we covered. (Demo: start a new conversation to try again.)"
)

WHY_VERIFY = (
    "Claim details are personal, so I can only share them once I've confirmed I'm speaking with "
    "the policyholder or someone they've authorised. It's the same for every caller."
)


def _list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _claim_line(c: dict[str, Any]) -> str:
    return (
        f"- {c['case_id']}: {c.get('case_type')} claim filed {P.fmt_date(c.get('created_at'))}, "
        f"{c.get('status')}"
    )


def _name(facts: dict[str, Any]) -> str:
    return (facts.get("caller") or {}).get("address_as") or ""


def _verified_prefix(facts: dict[str, Any]) -> str:
    if not facts.get("just_verified"):
        return ""
    name = _name(facts)
    return f"Thanks, {name} — you're verified. " if name else "Thank you — you're verified. "


def _core(d: P.Directive, state: SessionState, settings: Settings) -> str:
    f, a = d.facts, d.action
    if a == P.REQUEST_IDENTITY:
        if f["need_rep_name"]:
            return (
                "Thanks, those details check out. Since you're calling on someone's behalf, could "
                "I have your own full name and your relationship to the policyholder?"
            )
        who = "the policyholder's" if f["is_representative"] else "your"
        if f["nothing_provided"]:
            options = ID_OPTIONS if not f["is_representative"] else ID_OPTIONS.replace("your ", "their ")
            text = (
                f"To protect {who} information I need to verify identity before we look at any "
                f"claim. Could you give me any three of these: {options}?"
            )
        else:
            parts = []
            if f["matched"]:
                parts.append(f"Thanks, I've matched the {_list(f['matched'])}.")
            if f["new_mismatch"]:
                parts.append(
                    f"The {_list(f['new_mismatch'])} didn't match what we have on file — you're "
                    "welcome to double-check it or use a different identifier."
                )
            if f["options"]:
                parts.append(
                    f"I need {f['remaining_needed']} more to finish verifying: could you give me "
                    f"the {_list(f['options'][:3]) if len(f['options']) <= 3 else _list(f['options'])}"
                    + (" (any of these is fine)?" if len(f["options"]) > 1 else "?")
                )
            text = " ".join(parts) or f"I still need three identifiers to verify: {ID_OPTIONS}."
        if not f["can_still_verify"]:
            text += " With the details available I can't complete verification in this chat."
        if f.get("noted_for_later"):
            text = "I've noted why you're calling and we'll get to it right after this. " + text
        return text
    if a == P.REP_NOT_ON_FILE:
        return (
            "Thank you. To protect the policyholder's privacy, I can only discuss claim details "
            "with the policyholder or a representative they've authorised, and I don't see you "
            "listed on this policy. The policyholder can contact us directly, or a human "
            "representative can help set up authorisation."
        )
    if a == P.CONSENT_REQUESTED:
        return (
            "Thank you, those details check out. Because you're calling on the policyholder's "
            "behalf, I need their consent before sharing claim details — they decide who can see "
            "them. I've just sent a consent request by text to the phone number on file. Please "
            "let me know once they've approved it."
        )
    if a == P.CONSENT_PENDING:
        return (
            "The consent request hasn't been approved yet. Could the policyholder check their "
            "texts and approve it? Let me know when that's done and I'll check again."
        )
    if a == P.CONSENT_TIMEOUT:
        return (
            "I haven't received the policyholder's consent in time, so I'm not able to share claim "
            "details in this chat. The policyholder can contact us directly, or I can connect you "
            "with a human representative."
        )
    if a == P.CLOSE_UNVERIFIED:
        return (
            f"No problem. Nothing was shared today, and you're welcome to contact {COMPANY_NAME} "
            "claims support again any time. Take care."
        )
    if a == P.NO_CLAIMS:
        return (
            _verified_prefix(f)
            + "I don't see any claims on file for this policy. Is there anything else about "
            "claims I can help you with?"
        )
    if a == P.CLARIFY_CASE:
        claims = f["claims"]
        wanted = [c for c in claims if c["case_id"] in f["candidates"]] or claims
        intro = (
            "I found more than one claim that could fit:"
            if f["candidates"]
            else f"I see {len(claims)} claim{'s' if len(claims) != 1 else ''} on the account:"
        )
        return (
            _verified_prefix(f)
            + intro
            + "\n"
            + "\n".join(_claim_line(c) for c in wanted)
            + "\nWhich one are you calling about?"
        )
    if a == P.ANSWER_CASE:
        c = f["claim"]
        caller = f.get("caller") or {}
        whose = (
            f"{caller.get('policyholder_name')}'s"
            if caller.get("role") == "representative" and caller.get("policyholder_name")
            else "your"
        )
        lines = [
            _verified_prefix(f)
            + f"I've pulled up claim {c['case_id']}, {whose} {c.get('case_type')} claim filed "
            f"{P.fmt_date(c.get('created_at'))}. It is currently {c.get('status')}."
        ]
        if c.get("denial_reason"):
            lines.append(f"It was denied because {c['denial_reason']}.")
        if c.get("documents_needed"):
            lines.append(f"Documents needed: {_list(list(c['documents_needed']))}.")
        if c.get("appeal_deadline"):
            lines.append(f"The appeal deadline is {P.fmt_date(c['appeal_deadline'])}.")
        if f.get("path") == "payment_question":
            lines.append(
                f"Allowed maximum {P.fmt_money(c.get('allowed_max_amount'))}; expected reimbursement "
                f"{P.fmt_money(c.get('expected_reimbursement_amount'))}; paid so far "
                f"{P.fmt_money(c.get('net_pay'))}."
            )
        lines.append("Is there anything else you'd like to know about this claim?")
        return " ".join(lines)
    if a == P.OFFER_EMAIL and f.get("other_address_requested"):
        return (
            "For your security I can only send the summary to the email address on file "
            f"({f['masked_email']}), not to a different one. Would you like me to send it there, "
            "or skip it?"
        )
    if a == P.OFFER_EMAIL:
        if f.get("remembered_preference") == "skip":
            return (
                "Earlier you said you'd rather not get an email summary, so I'll skip it unless "
                "you've changed your mind. Would you like me to send a summary of this conversation "
                f"to {f['masked_email']} after all, or skip it?"
            )
        lead = (
            "Earlier you mentioned you'd like an email summary. "
            if f.get("remembered_preference") == "send"
            else ""
        )
        return (
            lead
            + "Before you go, would you like me to email a summary of this conversation — what we "
            f"discussed, the claim status and your next steps — to {f['masked_email']}? You can "
            "say send or skip."
        )
    if a == P.EMAIL_SENT:
        # Fixed wording uses only text built from the records, never the model-drafted summary.
        step = f.get("next_step_from_records")
        return (
            f"Done — I've sent the summary to {f['sent_to']}."
            + (f" The main next step: {step}" if step else "")
            + f" Thanks for contacting {COMPANY_NAME}, and take care."
        )
    if a == P.EMAIL_SKIPPED:
        if f.get("send_failed"):
            lead = "I'm sorry, I wasn't able to send the email, so nothing was sent."
        else:
            lead = "No problem, I won't send an email."
        return f"{lead} Thanks for contacting {COMPANY_NAME}, and take care."
    if a == P.CLOSED:
        return "You're welcome. If you have another claims question, just ask. Take care."
    if a == P.ESCALATE:
        reason = {
            "caller_requested": "Of course —",
            "verification_failed": (
                "For the security of the account I can't continue verification in this chat, so"
            ),
            "verification_refused": "I understand, and I respect that.",
            "repeated_out_of_scope": "I'm only able to help with insurance claims here, so",
            "turn_limit": "This conversation has reached the maximum length for one chat, so",
        }.get(f["reason"], "")
        tail = (
            " They'll need to verify your identity before discussing claim details."
            if not f.get("verified")
            else ""
        )
        return (
            f"{reason} I'm connecting you with a human representative now. I'm passing along a "
            f"short summary so you won't have to repeat everything.{tail}"
        ).strip()
    if a == P.ESCALATED_NOTICE:
        return ESCALATED_NOTICE
    return "I'm sorry, could you say that again?"


def template_reply(d: P.Directive, state: SessionState, settings: Settings) -> str:
    """Safe, fixed wording for a directive, including its modifiers."""
    if d.action == P.ESCALATED_NOTICE:
        return ESCALATED_NOTICE
    mods = d.mods
    prefix: list[str] = []
    escalating = d.action == P.ESCALATE
    if mods.empathy and not (escalating and d.facts.get("reason") == "verification_refused"):
        emotion = mods.empathy[0]
        prefix.append(
            {
                "confused": "Let me make that clearer.",
                "anxious": "I understand this is worrying, and I'll help you get it sorted.",
                "distressed": "I'm sorry you're going through this, and I'll help however I can.",
            }.get(emotion, "I understand this is frustrating, and I'm sorry for the hassle.")
        )
    if mods.decline_topic and not escalating:
        prefix.append(
            f"I'm not able to help with that here — I can only assist with {COMPANY_NAME} "
            "insurance claims."
        )
    if mods.explain_why and d.action in {P.REQUEST_IDENTITY, P.CONSENT_REQUESTED, P.CONSENT_PENDING}:
        prefix.append(WHY_VERIFY)
    text = " ".join(prefix + [_core(d, state, settings)])
    already_offers_human = {P.ESCALATE, P.REP_NOT_ON_FILE, P.CONSENT_TIMEOUT}
    if mods.ambiguous_yes and d.action != P.ESCALATE:
        text += (
            " Just so I do the right thing: would you like to carry on here, or shall I connect "
            "you with a human representative?"
        )
    elif mods.offer_human and d.action not in already_offers_human:
        text += " If you'd prefer, I can connect you with a human representative instead."
    return text
