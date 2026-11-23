"""Turn orchestrator: understand → decide (SOP engine) → phrase → guard.

    caller message
        │  1. understand   (model, structured; no authority)
        ▼
    Understanding ──► 2. policy.step   (deterministic: memory, gates, phase, tools, escalation)
                              │
                              ▼
                        Directive + facts
                              │  3. respond  (model phrases the move; tools limited by phase)
                              ▼
                        draft reply ──► 4. guard (leak + grounding checks) ──► caller

Failure handling follows from where the decision is made. Until step 2 has run, nothing has
been decided, so a model failure there aborts the turn and the conversation is left exactly
as it was (the caller's message can be resent). Once step 2 has run, the move is decided and
any tools have executed, so a failure while phrasing never rolls the turn back: the fixed
template wording for that directive is sent instead.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

from . import guard, policy, prompts, replies
from .config import Settings
from .data import Repository
from .llm import LLMError, LLMRefusal
from .state import STATUS_ESCALATED, Phase, SessionState, Trace
from .tools import Toolbox
from .understanding import Understanding, parse_understanding


class Backend(policy.PolicyBackend, Protocol):
    name: str

    def understand(
        self, state: SessionState, user_text: str, settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def respond(
        self,
        state: SessionState,
        directive: policy.Directive,
        control_block: str,
        tool_specs: list[dict[str, Any]],
        tool_executor: Any,
        settings: Settings,
    ) -> Any: ...


class ConversationClosed(Exception):
    """The conversation already holds the maximum number of messages (its last one was answered
    by a handoff to a human); nothing more is processed or stored."""


def _llm_label(meta: dict[str, Any]) -> str:
    parts = [str(meta.get("call", "model call")), str(meta.get("model", ""))]
    if meta.get("ms") is not None:
        parts.append(f"{meta['ms']} ms")
    return " · ".join(p for p in parts if p)


class Agent:
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    def new_session(self, consent_scenario: str = "default") -> SessionState:
        if consent_scenario not in self.repo.consent_scenarios:
            consent_scenario = "default"
        state = SessionState(session_id=uuid.uuid4().hex, consent_scenario=consent_scenario)
        state.phase_history.append(
            {"turn": 0, "from": None, "to": Phase.VERIFY_ID.value, "reason": "session_start"}
        )
        state.messages.append({"role": "assistant", "text": replies.GREETING, "turn": 0})
        return state

    def handle_turn(
        self, state: SessionState, user_text: str, backend: Backend
    ) -> tuple[str, dict[str, Any]]:
        """Process one caller message. Mutates `state`; callers pass a working copy and
        commit it only if this returns (an LLMError means: discard the copy, nothing happened).

        A conversation holds at most `max_turns_per_conversation` messages: the last one is
        answered by a handoff to a human without reading it, and any message after that raises
        ConversationClosed before anything changes."""
        started = time.perf_counter()
        cfg = self.settings
        limit = cfg.max_turns_per_conversation
        if state.turn >= limit:
            raise ConversationClosed(
                f"This conversation has reached its limit of {limit} messages and was handed to "
                "a human representative. Please start a new conversation."
            )
        state.turn += 1
        trace = Trace(turn=state.turn, phase_before=state.phase.value)
        tools = Toolbox(self.repo, state, trace, cfg)

        if state.status == STATUS_ESCALATED:
            state.messages.append({"role": "user", "text": user_text, "turn": state.turn})
            trace.add("gate", "conversation already handed to a human — no further processing", None)
            return self._finish(state, trace, replies.ESCALATED_NOTICE, started)

        if state.turn >= limit:
            # The last message one conversation may hold: close it with a handoff, the same
            # way as the other handoffs, instead of letting it (and its cost) grow without end.
            state.messages.append({"role": "user", "text": user_text, "turn": state.turn})
            directive = policy.close_at_turn_limit(state, tools, backend, trace, cfg, self.repo)
            return self._finish(state, trace, self._reply(state, directive, tools, backend, trace), started)

        # 1. Understand (the model reads; it decides nothing).
        try:
            raw, meta = backend.understand(state, user_text, cfg)
            u = parse_understanding(raw)
            trace.add("llm", _llm_label(meta), meta)
        except LLMRefusal as exc:
            if exc.kind != "refusal":
                # No reading at all (truncated or unparseable output, after a retry). Acting on
                # a blank reading would move counters and polls on a message nobody understood,
                # so the turn is abandoned instead and can be retried.
                raise LLMError(
                    "llm_error",
                    "The model returned an unusable reading of that message; nothing was "
                    "recorded. Please send it again.",
                    502,
                ) from exc
            # The model (or a safety classifier) declined the message itself: treat it as
            # something this line cannot help with.
            u = Understanding(
                summary="(the model declined to read this message)",
                scope="out_of_scope",
                out_of_scope_topic="that request",
                degraded=True,
            )
            trace.add("error", "the model declined this message — treated as out of scope", None)
        trace.add("understand", u.summary or "caller message read", u.trace_view())
        state.messages.append({"role": "user", "text": user_text, "turn": state.turn})

        # 2. Decide (deterministic SOP engine).
        directive = policy.step(state, u, tools, backend, trace, cfg, self.repo)

        # 3 + 4. Phrase and guard.
        reply = self._reply(state, directive, tools, backend, trace)
        return self._finish(state, trace, reply, started)

    # -- internals ---------------------------------------------------------------
    def _finish(
        self, state: SessionState, trace: Trace, reply: str, started: float
    ) -> tuple[str, dict[str, Any]]:
        state.messages.append({"role": "assistant", "text": reply, "turn": state.turn})
        trace.phase_after = state.phase.value
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        record = trace.to_dict()
        state.traces.append(record)
        return reply, record

    def _grounding_claims(self, state: SessionState, directive: policy.Directive) -> list[dict[str, Any]]:
        """Records the reply may draw figures from: the selected claim and those already discussed."""
        records = []
        if directive.facts.get("claim"):
            records.append(directive.facts["claim"])
        if state.verified:
            for case_id in state.cases_discussed:
                claim = self.repo.claim(case_id)
                if claim and claim.get("party_id") == state.party_id:
                    records.append(self.repo.claim_public_record(claim))
        return records

    def _reply(
        self,
        state: SessionState,
        directive: policy.Directive,
        tools: Toolbox,
        backend: Backend,
        trace: Trace,
    ) -> str:
        cfg = self.settings
        template = replies.template_reply(directive, state, cfg)
        if directive.deterministic:
            return template

        control = prompts.render_control_block(directive, state, cfg)
        # Tools are offered only where the SOP lists them for the model (PROCESS_CASE).
        specs = tools.llm_tool_specs() if directive.action == policy.ANSWER_CASE else []

        def generate(block: str) -> str | None:
            try:
                result = backend.respond(state, directive, block, specs, tools.execute_llm_tool, cfg)
            except (LLMRefusal, LLMError) as exc:
                # The move is already decided and its tools have run; only the wording failed.
                trace.add("error", f"reply unavailable ({exc}) — using the template wording", None)
                return None
            meta = dict(result.meta)
            if result.tool_calls:
                meta["tool_calls"] = result.tool_calls
            trace.add("llm", _llm_label(meta), meta)
            return result.text

        text = generate(control)
        if text is None:
            return template

        owned = tools.owned_case_ids()
        claims = self._grounding_claims(state, directive)
        hard, soft = guard.reply_violations(text, state, self.repo, claims, owned)
        if hard:
            state.guard_blocks += 1
            trace.add("guard", "reply blocked — template wording sent instead", hard)
            return template
        if soft:
            trace.add("guard", "reply not grounded — regenerating once", soft)
            retry = generate(
                control
                + "\n<correction>Your previous draft was rejected because it contained: "
                + "; ".join(soft)
                + ". State only amounts and claim numbers that appear in the control block or "
                "tool results; refer to a figure the caller mentioned as 'that amount' rather "
                "than repeating it.</correction>"
            )
            if retry is not None:
                # Tool calls made while regenerating may have added grounded data.
                claims = self._grounding_claims(state, directive)
                hard, soft = guard.reply_violations(retry, state, self.repo, claims, owned)
            if retry is None or hard or soft:
                state.guard_blocks += 1
                trace.add("guard", "regenerated reply still rejected — template wording sent", hard + soft)
                return template
            text = retry
        trace.add("guard", "reply passed leak, verification-claim and grounding checks", None)
        return text
