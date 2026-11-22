"""Offline test double for the model (SOP_LLM_MODE=mock).

Keyword/regex 'understanding' and template replies. It is deliberately crude —
its job is to let the harness (gates, memory, transitions, tools, guard) be
exercised deterministically in tests and without an API key. It is not a
substitute for the model's language understanding.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import identity, policy as P, replies
from .config import Settings
from .state import SessionState
from .tools import ToolError

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
_POLICY = re.compile(r"\bPOL[- ]?\d{3,}\b", re.I)
_CASE = re.compile(r"\bCL[- ]?(\d{3,})\b", re.I)
_PHONE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_LAST4 = re.compile(r"(?:ssn|social|national id|last\s+(?:four|4)|ending in)\D{0,40}?(\d{4})(?!\d)", re.I)
_DATES = [
    re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b"),
    re.compile(r"\b[A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)? (?:of )?[A-Za-z]{3,9}\.?,? \d{4}\b"),
]
# Prefix is case-insensitive; the name itself must be capitalised words.
_NAME = re.compile(
    r"(?i:my (?:full )?name is|name's|this is|i am|i'm|it's|name:)\s+"
    r"((?:[A-Z][a-zA-Z'’-]+\s+){1,3}[A-Z][a-zA-Z'’-]+)"
)
_BEHALF = re.compile(
    r"(?i:on behalf of|calling for|for)\s+(?:my\s+(\w+)\s+)?((?:[A-Z][a-zA-Z'’-]+\s+){1,3}[A-Z][a-zA-Z'’-]+)"
)
_REP_WORDS = re.compile(
    r"on behalf of|calling for my|for my (mother|father|mom|dad|wife|husband|son|daughter|parent|client|patient)"
    r"|my (mother|father|mom|dad|wife|husband|son|daughter)'s (claim|policy)",
    re.I,
)
_MONTHS = (
    "january february march april june july august september october november december".split()
)

_IN_SCOPE = re.compile(
    r"claim|policy|insur|denied|denial|appeal|document|submit|upload|pay|paid|reimburs|verif|identity"
    r"|ssn|social|birth|dob|e-?mail|phone|name|representative|human|agent|deadline|status|pathology"
    r"|report|office note|coverage|summary|consent|approv|portal|fax|processing|review|receipt"
    r"|confirm|supervisor|person|records?|provider|estimate|photo|amount|owe|bill|check|texts?|waiting",
    re.I,
)
_OOS = re.compile(
    r"\brl\b|reinforcement learning|weather|joke|recipe|python|javascript|write (?:me )?(?:a |some )?code"
    r"|capital of|who won|poem|ignore (?:all |your |previous )|bitcoin|stock market|movie|football"
    r"|meaning of life|translate|homework|president",
    re.I,
)
_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|please do|go ahead|absolutely|correct|that's right|send)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|nah|not now|don't|do not|skip)\b", re.I)
_END = re.compile(
    r"that'?s all|that is all|nothing else|no,? thanks|no thank you|goodbye|\bbye\b|that'?s it(?![?\w])"
    r"|i'?m done|all set|that will be all|nothing more|i'?m good(?=\s*(?:[.!,]|$|thanks|thank you))",
    re.I,
)
_PERSON = r"(?:real |live |human )?(?:human|person|representative|agent|supervisor|manager|operator)"
_HUMAN = re.compile(
    rf"\b(?:speak|talk|chat)\s+(?:to|with)\s+(?:(?:a|an|the)\s+)?(?:{_PERSON}|someone|somebody)\b"
    rf"|\b(?:transfer|connect|put)\s+me\b"
    rf"|\b(?:get|give|want|need)\s+(?:me\s+)?an?\s+{_PERSON}\b"
    rf"|^\s*(?:an?\s+)?{_PERSON}(?:,? please)?[.!]?\s*$",
    re.I,
)
_FULL_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-(\d{4})(?!\d)")
_DOC_TOPICS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"don'?t have|can'?t get|cannot get|unable to get|lost|alternative|instead of", re.I), "missing_document_alternatives"),
    (re.compile(r"how long|processing time|review time|after i (submit|send)|once i (submit|send)", re.I), "processing_time_after_submission"),
    (re.compile(r"how soon|when (do|should) i (submit|send)", re.I), "submission_timing"),
    (re.compile(r"format|file type|pdf|scan|photo|image", re.I), "file_format_requirements"),
    (re.compile(r"confirmation|confirm receipt|know you got|show up", re.I), "receipt_confirmation"),
    (re.compile(r"how (do|should|can) i (submit|send|upload)|where (do|should) i (submit|send)|portal|upload", re.I), "submission_method"),
    (re.compile(r"what (does|should|must).*(contain|include|show)|requirements?", re.I), "document_requirements"),
]


def _find_dob(text: str) -> tuple[str, str] | None:
    """(ISO date, matched text) for the first date that looks like a birth date."""
    for pattern in _DATES:
        for m in pattern.finditer(text):
            iso = identity.normalize_dob(m.group(0))
            if iso and int(iso[:4]) < 2015:  # crude: a birth date, not a claim date
                return iso, m.group(0)
    return None


class MockBackend:
    name = "mock"

    # -- understand ------------------------------------------------------------
    def understand(
        self, state: SessionState, user_text: str, settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        t = user_text.strip()
        low = t.lower()
        last_agent = state.last_assistant_text().lower()

        ident = {"full_name": "", "dob": "", "phone": "", "email": "", "id_last4": "", "policy_number": ""}
        if m := _EMAIL.search(t):
            ident["email"] = m.group(0)
        if m := _POLICY.search(t):
            ident["policy_number"] = m.group(0)
        rest = low  # the message with the birth date removed, for claim-date hints
        if dob := _find_dob(t):
            ident["dob"] = dob[0]
            rest = low.replace(dob[1].lower(), " ")
        if m := _FULL_SSN.search(t) or _LAST4.search(t):
            ident["id_last4"] = m.group(1)
        if m := _PHONE.search(t):
            ident["phone"] = m.group(0)

        role, rep_name, relationship = "unknown", "", ""
        name = _NAME.search(t)
        if _REP_WORDS.search(t):
            role = "representative"
            if name:
                rep_name = name.group(1)
            if b := _BEHALF.search(t):
                ident["full_name"] = b.group(2)
                if b.group(1):
                    relationship = f"policyholder is the caller's {b.group(1)}"
        else:
            if name:
                ident["full_name"] = name.group(1)
            if re.search(r"\bi(?:'m| am) the policy ?holder|my own (policy|claim)", low):
                role = "policyholder"

        hints = {"case_id": "", "case_type": "", "status": "", "time_reference": "", "description": ""}
        if m := _CASE.search(t):
            hints["case_id"] = f"CL-{m.group(1)}"
        if "claim" in low or hints["case_id"] or state.verified:
            if re.search(r"health|medical|hospital|doctor", low):
                hints["case_type"] = "healthcare"
            elif re.search(r"dental|dentist|tooth|teeth", low):
                hints["case_type"] = "dental"
            elif re.search(r"\bauto\b|\bcar\b|vehicle|accident|collision", low):
                hints["case_type"] = "auto"
            if re.search(r"denied|rejected|denial|turned down", low):
                hints["status"] = "denied"
            elif re.search(r"\bopen\b|pending|in progress|processing", low):
                hints["status"] = "open"
            elif re.search(r"closed|settled|completed|paid out", low):
                hints["status"] = "closed"
            month = next((mo for mo in _MONTHS if re.search(rf"\b{mo}\b", rest)), "")
            year = re.search(r"\b(20[12]\d)\b", rest)
            if month:
                hints["time_reference"] = month.capitalize() + (f" {year.group(1)}" if year else "")
            elif year:
                hints["time_reference"] = year.group(1)
            elif "last month" in low:
                hints["time_reference"] = "last month"

        intent = "none"
        if re.search(r"why.{0,40}(denied|rejected)|appeal|dispute|reason.{0,20}deni", low):
            intent = "denial_question"
        elif re.search(r"document|paperwork|submit|upload|send in|pathology|office note|\bfax\b|portal", low):
            intent = "document_submission"
        elif re.search(r"how much|reimburs|\bpaid\b|payment|amount|\bowe\b", low):
            intent = "payment_question"
        elif re.search(r"next step|what (do|should) i do|what now|what happens next", low):
            intent = "next_steps"
        elif re.search(r"status|where is my claim|update on|check on", low):
            intent = "status_inquiry"
        elif state.verified and "claim" in low and "?" in t:
            intent = "general_claim_question"

        yes_no = "yes" if _YES.search(t) else "no" if _NO.search(t) else "none"

        email_choice = "none"
        if re.search(r"(e-?mail|send) me (a|the) (summary|recap)|send (it|the summary|the email)", low):
            email_choice = "send"
        elif re.search(r"no e-?mail|don'?t (e-?mail|send)|skip (it|the e-?mail|the summary)", low):
            email_choice = "skip"
        elif "summary" in last_agent and "email" in last_agent and yes_no != "none":
            email_choice = "send" if yes_no == "yes" else "skip"
        other_address = bool(
            re.search(r"(different|another|other|new|work|personal) (e-?mail|address)", low)
            or (re.search(r"send (it|that|the summary)?\s*to\b", low) and _EMAIL.search(t))
        )
        if other_address:
            email_choice = "none"

        # Only an explicit request counts. A bare "yes" after an offer is left to the engine,
        # which treats it as ambiguous and asks.
        requests_human = bool(_HUMAN.search(t))

        wants_to_end = bool(_END.search(low)) or (yes_no == "no" and "anything else" in last_agent)

        refused_fields: list[str] = []
        refusal = "none"
        if re.search(r"just tell me|already told you|not going to verify|won'?t verify|refuse to verify"
                     r"|don'?t need to verify|skip (the )?verification|not giving you (anything|any)", low):
            refusal = "identity_info"
        elif m := re.search(r"(not comfortable|don'?t want|won'?t|not giving|rather not|refuse)"
                            r".{0,50}(ssn|social|birth|dob|phone|e-?mail|name)", low):
            refusal = "specific_field"
            key = m.group(2)
            refused_fields = [
                "id_last4" if key in {"ssn", "social"}
                else "dob" if key in {"birth", "dob"}
                else "phone" if key == "phone"
                else "email" if key.startswith("e") else "full_name"
            ]
            if refused_fields[0] == "id_last4":
                ident["id_last4"] = ""

        emotion, intensity = "neutral", "none"
        if re.search(r"furious|outrageous|worst|terrible|\bhell\b|damn|!!+", low):
            emotion, intensity = "angry", "strong"
        elif re.search(r"ridiculous|frustrat|annoy|unacceptable|waste of (my )?time|already told you|fed up|sick of", low):
            emotion, intensity = "frustrated", "moderate"
        elif re.search(r"worried|anxious|scared|nervous|stress", low):
            emotion, intensity = "anxious", "moderate"
        elif re.search(r"confus|don'?t understand|what do you mean", low):
            emotion, intensity = "confused", "mild"

        has_identity = any(ident.values())
        in_scope_signal = (
            has_identity
            or any(hints.values())
            or intent != "none"
            or bool(_IN_SCOPE.search(low))
            or yes_no != "none"
            or wants_to_end
            or emotion != "neutral"
            or bool(re.match(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening))\b", low))
        )
        oos_signal = bool(_OOS.search(low)) or (
            not in_scope_signal
            and len(low.split()) >= 3
            and bool(re.search(r"\?|^\s*(what|who|how|why|when|where|tell me|explain)\b", low))
        )
        if oos_signal and (has_identity or any(hints.values()) or intent != "none"):
            scope = "mixed"
        elif oos_signal:
            scope = "out_of_scope"
        else:
            scope = "in_scope"

        different = False
        if state.selected_case_id and state.verified:
            if re.search(r"(another|different|other) claim", low):
                different = True
            elif hints["case_id"] and hints["case_id"] != state.selected_case_id:
                different = True
            elif hints["case_type"] and hints["case_type"] not in (state.selected_case_brief or ""):
                different = True

        # Describe the message by kind, never by value (identifier values stay out of the trace).
        gave = [k for k, v in ident.items() if v]
        summary = "; ".join(
            part
            for part in (
                f"gave {', '.join(gave)}" if gave else "",
                "mentioned a claim" if any(hints.values()) else "",
                f"intent {intent}" if intent != "none" else "",
                f"scope {scope}" if scope != "in_scope" else "",
                f"emotion {emotion}" if emotion != "neutral" else "",
            )
            if part
        ) or "short reply"
        raw = {
            "summary": f"(mock reading) {summary}",
            "identity": ident,
            "caller": {"role": role, "representative_name": rep_name, "relationship": relationship},
            "claim_hints": hints,
            "intent": intent,
            "scope": scope,
            "out_of_scope_topic": "an unrelated topic" if scope != "in_scope" else "",
            "emotion": emotion,
            "emotion_intensity": intensity,
            "refusal": refusal,
            "refused_fields": refused_fields,
            "asks_why": bool(re.search(r"why (do|should|must) (you|i)|why is (this|that)|what for\b", low)),
            "requests_human": requests_human,
            "wants_to_end": wants_to_end,
            "wants_different_claim": different,
            "yes_no": yes_no,
            "email_summary_choice": email_choice,
            "email_to_other_address": other_address,
            "notes": [],
        }
        return raw, {"call": "understand", "model": "mock"}

    # -- resolve -----------------------------------------------------------------
    def resolve(
        self, state: SessionState, claims: list[dict[str, Any]], settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        hints = {k: v.value.lower() for k, v in state.hints.items()}
        texts = state.user_texts()
        last = texts[-1].lower() if texts else ""
        pool = list(claims)

        def narrow(pred: Callable[[dict[str, Any]], bool]) -> None:
            nonlocal pool
            kept = [c for c in pool if pred(c)]
            if kept:
                pool = kept

        if hints.get("case_id"):
            pool = [c for c in claims if c["case_id"].lower() == hints["case_id"]]
        else:
            if hints.get("case_type"):
                pool = [c for c in pool if c.get("case_type") == hints["case_type"]]
            if hints.get("status"):
                pool = [c for c in pool if c.get("status") == hints["status"]]
            ref = hints.get("time_reference", "")
            for i, month in enumerate(
                "january february march april may june july august september october november december".split(),
                start=1,
            ):
                if month in ref:
                    pool = [c for c in pool if (c.get("created_at") or "")[5:7] == f"{i:02d}"]
            if y := re.search(r"20\d\d", ref):
                pool = [c for c in pool if (c.get("created_at") or "").startswith(y.group(0))]
            # A pick from options the agent just listed.
            listed = [c for c in claims if c["case_id"] in state.candidates] or pool
            for word, idx in (("first", 0), ("second", 1), ("third", 2), ("last", -1)):
                if re.search(rf"\b{word}\b", last) and len(listed) > abs(idx) - (idx < 0):
                    pool = [listed[idx]]
            if y := re.search(r"\b(20\d\d)\b", last):
                narrow(lambda c: (c.get("created_at") or "").startswith(y.group(1)))
            for status in ("denied", "open", "closed"):
                if re.search(rf"\b{status}\b", last):
                    narrow(lambda c, s=status: c.get("status") == s)
            if re.search(r"\b(recent|latest|newest|newer)\b", last) and len(pool) > 1:
                pool = [max(pool, key=lambda c: c.get("created_at") or "")]
            if re.search(r"\b(older|oldest|earlier)\b", last) and len(pool) > 1:
                pool = [min(pool, key=lambda c: c.get("created_at") or "")]

        intent = state.intent.value if state.intent else "none"
        if len(pool) == 1:
            chosen = pool[0]
            if intent == "none" and chosen.get("status") == "denied" and hints.get("status") == "denied":
                intent = "denial_question"
            return (
                {"case_id": chosen["case_id"], "candidate_case_ids": [], "intent": intent,
                 "rationale": "exactly one claim fits the caller's description"},
                {"call": "resolve", "model": "mock"},
            )
        candidates = [c["case_id"] for c in pool] if 1 < len(pool) < len(claims) else []
        return (
            {"case_id": "", "candidate_case_ids": candidates, "intent": intent,
             "rationale": "ambiguous or no match" if pool else "nothing on the account fits"},
            {"call": "resolve", "model": "mock"},
        )

    # -- summarize -----------------------------------------------------------------
    def summarize(
        self, state: SessionState, records: list[dict[str, Any]], settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return P.deterministic_summary(records), {"call": "summarize", "model": "mock"}

    # -- respond ---------------------------------------------------------------------
    def respond(
        self,
        state: SessionState,
        directive: P.Directive,
        control_block: str,
        tool_specs: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        settings: Settings,
    ) -> Any:
        from .llm import RespondResult

        calls: list[dict[str, Any]] = []
        text = replies.template_reply(directive, state, settings)
        texts = state.user_texts()
        last = texts[-1] if texts else ""
        tool_names = {spec["name"] for spec in tool_specs}
        if (
            directive.action == P.ANSWER_CASE
            and "get_document_guidance" in tool_names
            and not directive.facts.get("just_resolved")
        ):
            topic = next((name for pattern, name in _DOC_TOPICS if pattern.search(last)), None)
            if topic:
                args = {"case_id": directive.facts["claim"]["case_id"], "topic": topic, "document": ""}
                try:
                    out = tool_executor("get_document_guidance", args)
                    calls.append({"name": "get_document_guidance", "args": args, "ok": True})
                    passages = " ".join(p["text"] for p in out.get("guidance", [])[:2])
                    text = f"{passages} Is there anything else you'd like to know about this claim?"
                except ToolError as exc:
                    calls.append({"name": "get_document_guidance", "args": args, "ok": False, "error": str(exc)})
        return RespondResult(text=text, tool_calls=calls, meta={"call": "respond", "model": "mock"})
