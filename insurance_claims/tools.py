"""Bounded tools.

Every action that touches records or the outside world goes through this
toolbox. Each call is checked against the SOP (`sop.check_tool`) for the
current phase and caller (harness vs. LLM), scoped to the verified
policyholder, and written to the turn trace.
"""

from __future__ import annotations

import json
from typing import Any

from . import identity, sop
from .config import Settings
from .data import Repository
from .state import SessionState, Trace


class ToolError(Exception):
    """A tool refused or failed; the message is safe to show to the model."""


GUIDANCE_TOPICS = (
    "document_requirements",
    "submission_method",
    "submission_timing",
    "processing_time_after_submission",
    "file_format_requirements",
    "receipt_confirmation",
    "missing_document_alternatives",
    "other",
)

# Tool definitions offered to the LLM (only in phases whose spec lists them).
LLM_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_document_guidance": {
        "name": "get_document_guidance",
        "description": (
            "Look up Harbor Mutual's official guidance about the documents requested for one "
            "of the caller's claims: what each document must contain, how and where to submit "
            "it, how soon, accepted file formats, how receipt is confirmed, how long review "
            "takes after submission, and what to do when a document cannot be obtained. Call "
            "this before answering any such question instead of answering from memory. "
            "Returns the applicable guidance passages verbatim."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "Claim number the question is about, as given in the selected claim record.",
                },
                "topic": {
                    "type": "string",
                    "enum": list(GUIDANCE_TOPICS),
                    "description": "Which kind of guidance is needed. Use 'other' if none fits.",
                },
                "document": {
                    "type": "string",
                    "description": (
                        "One specific requested document the caller asked about (e.g. "
                        "'pathology report'), or an empty string for all requested documents."
                    ),
                },
            },
            "required": ["case_id", "topic", "document"],
            "additionalProperties": False,
        },
    },
    "list_claims": {
        "name": "list_claims",
        "description": (
            "List the verified policyholder's claims (number, type, filing date, status, "
            "one-line summary). Use when the caller asks what other claims they have."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def _join(items: list[str]) -> str:
    items = [f"the {i}" for i in items]
    if not items:
        return "the requested documents"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _lookup_doc(table: dict[str, Any], doc: str) -> tuple[str, str] | None:
    """Find guidance for a document name, tolerating wording differences
    ('pathology report' vs 'original pathology report')."""
    wanted = doc.strip().lower()
    for key, value in table.items():
        k = key.lower()
        if k == "default":
            continue
        if wanted == k or wanted in k or k in wanted:
            return key, value.get("en", "") if isinstance(value, dict) else str(value)
    return None


class Toolbox:
    def __init__(
        self, repo: Repository, state: SessionState, trace: Trace, settings: Settings
    ) -> None:
        self.repo = repo
        self.state = state
        self.trace = trace
        self.settings = settings

    # -- plumbing ------------------------------------------------------------
    def _authorize(self, tool: str, actor: str) -> None:
        try:
            sop.check_tool(self.state.phase, tool, actor)
        except sop.SOPViolation as exc:
            self.trace.add("guard", f"blocked tool call: {tool} ({actor})", {"reason": str(exc)})
            raise ToolError(str(exc)) from exc

    def _owned_claim(self, case_id: str) -> dict[str, Any]:
        if not self.state.verified or not self.state.party_id:
            raise ToolError("identity is not verified")
        claim = self.repo.claim(case_id)
        if claim is None or claim.get("party_id") != self.state.party_id:
            # Same message for "does not exist" and "belongs to someone else".
            raise ToolError(f"no claim {case_id} on this policyholder's account")
        return claim

    def _ground(self, payload: Any) -> None:
        """Record data shown to the model, so the guard can check replies against it."""
        text = json.dumps(payload)
        if text not in self.state.grounding:
            self.state.grounding.append(text)

    def owned_case_ids(self) -> set[str]:
        if not self.state.verified:
            return set()
        return {c["case_id"] for c in self.repo.claims_for(self.state.party_id)}

    # -- VERIFY_ID (harness only) ---------------------------------------------
    def verify_identity(self) -> identity.VerificationResult:
        self._authorize("verify_identity", sop.HARNESS)
        result = identity.evaluate(self.state.identity_claims, self.repo)
        self.trace.add(
            "tool",
            f"verify_identity → {result.matched_count} of "
            f"{self.settings.required_id_matches} required identifiers matched",
            result.to_dict(),
        )
        return result

    def lookup_representative(self, party_id: str) -> dict[str, Any]:
        self._authorize("lookup_representative", sop.HARNESS)
        name = self.state.rep_name or ""
        match = next(
            (
                r
                for r in self.repo.representatives_for(party_id)
                if identity.names_match(name, r.get("rep_name", ""))
            ),
            None,
        )
        out = {"on_file": match is not None, "relationship": match.get("relationship") if match else None}
        self.trace.add(
            "tool",
            "lookup_representative → "
            + ("authorized representative on file" if match else "not on file"),
            out,
        )
        return out

    def _consent_sequence(self) -> list[str]:
        scenario = self.repo.consent_scenarios.get(
            self.state.consent_scenario
        ) or self.repo.consent_scenarios.get("default", {})
        return list(scenario.get("status_sequence") or ["pending", "approved"])

    def request_consent(self) -> str:
        """Ask the policyholder (simulated out-of-band channel) to approve the representative."""
        self._authorize("request_consent", sop.HARNESS)
        seq = self._consent_sequence()
        self.state.consent_polls = 0
        status = seq[0]
        self.trace.add(
            "tool",
            f"request_consent → {status}",
            {"scenario": self.state.consent_scenario, "channel": "text message to phone on file"},
        )
        return status

    def check_consent_status(self) -> str:
        self._authorize("check_consent_status", sop.HARNESS)
        seq = self._consent_sequence()
        self.state.consent_polls += 1
        status = seq[min(self.state.consent_polls, len(seq) - 1)]
        limit = min(len(seq) - 1, self.settings.consent_max_polls)
        if status != "approved" and self.state.consent_polls >= limit:
            status = "timed_out"
        self.trace.add(
            "tool",
            f"check_consent_status → {status}",
            {"poll": self.state.consent_polls, "scenario": self.state.consent_scenario},
        )
        return status

    # -- claims ----------------------------------------------------------------
    def list_claims(self, actor: str = sop.HARNESS) -> list[dict[str, Any]]:
        self._authorize("list_claims", actor)
        if not self.state.verified:
            raise ToolError("identity is not verified")
        index = [self.repo.claim_index_entry(c) for c in self.repo.claims_for(self.state.party_id)]
        index.sort(key=lambda c: c.get("created_at") or "", reverse=True)
        self.state.claims_on_file = len(index)
        self.trace.add("tool", f"list_claims ({actor}) → {len(index)} on file", index)
        if actor == sop.LLM:
            self._ground(index)
        return index

    def get_claim_details(self, case_id: str) -> dict[str, Any]:
        self._authorize("get_claim_details", sop.HARNESS)
        record = self.repo.claim_public_record(self._owned_claim(case_id))
        self.trace.add("tool", f"get_claim_details({record['case_id']})", record)
        self._ground(record)
        return record

    def get_document_guidance(
        self, case_id: str, topic: str, document: str = "", actor: str = sop.LLM
    ) -> dict[str, Any]:
        self._authorize("get_document_guidance", actor)
        claim = self._owned_claim(case_id)
        if topic not in GUIDANCE_TOPICS:
            topic = "other"
        g = self.repo.guideline
        docs: list[str] = list(claim.get("documents_needed") or [])
        focus = docs
        if document.strip():
            wanted = document.strip().lower()
            narrowed = [d for d in docs if wanted in d.lower() or d.lower() in wanted]
            focus = narrowed or docs
        passages: list[dict[str, str]] = []

        def add(source: str, text: str | None) -> None:
            if text and not any(p["text"] == text for p in passages):
                passages.append({"source": source, "text": text})

        fmt = {
            "case_id": claim["case_id"],
            "documents": _join(focus),
            "average_processing_time_after_submission": g.get("claim_followup_settings", {})
            .get("average_processing_time_after_submission", {})
            .get("en", ""),
        }
        followups = {f["topic"]: f for f in g.get("claim_followup_guidance", [])}

        def add_followup(name: str) -> None:
            item = followups.get(name)
            if not item or (item.get("requires_documents") and not docs):
                return
            add(f"claim_followup_guidance/{name}", item["en"].format(**fmt))

        def add_doc_specific(table_name: str) -> None:
            table = g.get(table_name, {})
            for d in focus:
                hit = _lookup_doc(table, d)
                if hit:
                    add(f"{table_name}/{hit[0]}", hit[1])

        case_type_text = g.get("case_type_guidance", {}).get(claim.get("case_type"), {}).get("en")
        default_text = g.get("default_guidance", {}).get("en")

        if topic == "document_requirements":
            add_doc_specific("document_guidance")
            add(f"case_type_guidance/{claim.get('case_type')}", case_type_text)
            add("default_guidance", default_text)
        elif topic == "submission_method":
            add_followup("submission_method")
            add("default_guidance", default_text)
            add(f"case_type_guidance/{claim.get('case_type')}", case_type_text)
        elif topic == "file_format_requirements":
            add_followup("file_format_requirements")
            add_doc_specific("document_guidance")
            add("default_guidance", default_text)
        elif topic == "missing_document_alternatives":
            before = len(passages)
            add_doc_specific("document_alternative_guidance")
            if len(passages) == before:
                add(
                    "document_alternative_guidance/default",
                    g.get("document_alternative_guidance", {}).get("default", {}).get("en"),
                )
            add_followup("missing_required_material_alternatives")
            add(
                "claim_followup_settings/human_review",
                g.get("claim_followup_settings", {})
                .get("human_review_after_document_alternatives_exhausted", {})
                .get("en"),
            )
        elif topic in followups:
            add_followup(topic)
        if not passages:
            add("claim_followup_fallback", g.get("claim_followup_fallback", {}).get("en"))

        out: dict[str, Any] = {
            "case_id": claim["case_id"],
            "topic": topic,
            "documents_requested_on_claim": docs,
            "guidance": passages,
        }
        if not docs:
            out["note"] = "No documents are currently requested on this claim."
        if claim.get("appeal_deadline"):
            out["appeal_deadline_on_claim"] = claim["appeal_deadline"]
        self.trace.add(
            "tool",
            f"get_document_guidance({claim['case_id']}, {topic}"
            + (f", {document}" if document.strip() else "")
            + f") → {len(passages)} passage(s)",
            out,
        )
        self._ground(out)
        return out

    # -- POST_PROCESS ------------------------------------------------------------
    def send_summary_email(self, subject: str, body: str) -> dict[str, Any]:
        self._authorize("send_summary_email", sop.HARNESS)
        if self.state.email_decision != "send":
            self.trace.add("guard", "blocked send_summary_email: no explicit consent", None)
            raise ToolError("the caller has not agreed to receive the email")
        holder = self.repo.policyholder(self.state.party_id)
        if not self.state.verified or holder is None or not holder.email:
            raise ToolError("no verified email address on file")
        masked = identity.mask_email(holder.email)
        # Demo transport: the message is placed in an in-memory outbox shown in the UI.
        self.state.outbox.append(
            {"to": masked, "subject": subject, "body": body, "sent_turn": self.state.turn}
        )
        out = {"status": "sent", "to": masked}
        self.trace.add("tool", f"send_summary_email → sent to {masked}", {"subject": subject})
        return out

    # -- dispatcher for model-initiated calls --------------------------------------
    def execute_llm_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_document_guidance":
            return self.get_document_guidance(
                str(args.get("case_id", "")),
                str(args.get("topic", "other")),
                str(args.get("document", "") or ""),
                actor=sop.LLM,
            )
        if name == "list_claims":
            return {"claims": self.list_claims(actor=sop.LLM)}
        self.trace.add("guard", f"blocked unknown tool call: {name}", None)
        raise ToolError(f"unknown tool '{name}'")

    def llm_tool_specs(self) -> list[dict[str, Any]]:
        """Tool definitions the LLM may use in the current phase (stable order)."""
        allowed = sop.SOP[self.state.phase].llm_tools
        return [LLM_TOOL_SPECS[name] for name in sorted(allowed) if name in LLM_TOOL_SPECS]
