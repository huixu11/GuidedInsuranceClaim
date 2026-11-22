"""Claude backend (Anthropic SDK).

Four operations, all on the Messages API:

* understand / resolve / summarize — structured outputs (`output_config.format`),
  so the result always parses against the schema the harness expects.
* respond — free text, with an optional tool loop restricted to the tools the
  SOP allows in the current phase.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from . import prompts
from .config import Settings, server_has_credentials
from .state import SessionState
from .tools import ToolError
from .understanding import RESOLUTION_SCHEMA, SUMMARY_SCHEMA, UNDERSTANDING_SCHEMA

log = logging.getLogger("sop.llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 4


class LLMError(Exception):
    """A model call failed in a way the user should hear about (auth, quota, outage)."""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class LLMRefusal(Exception):
    """No usable output for this request.

    kind="refusal": the model or a safety classifier declined.
    kind="unusable": output was empty, truncated or did not parse.
    """

    def __init__(self, what: str, kind: str = "refusal") -> None:
        super().__init__(what)
        self.kind = kind


@dataclass
class RespondResult:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _usage(resp: Any) -> dict[str, Any]:
    u = getattr(resp, "usage", None)
    return {
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
    }


def _text(resp: Any) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, settings: Settings, api_key: str | None = None) -> None:
        self.settings = settings
        if not api_key and not server_has_credentials():
            raise LLMError(
                "missing_api_key",
                "No model API key is configured. Set ANTHROPIC_API_KEY on the server or enter a "
                "key in the UI.",
                401,
            )
        kwargs: dict[str, Any] = {"max_retries": 2, "timeout": 120.0}
        if api_key:
            kwargs["api_key"] = api_key
        self.client = anthropic.Anthropic(**kwargs)
        self._fallbacks_rejected = False

    # -- transport -----------------------------------------------------------
    def _create(self, **kwargs: Any) -> Any:
        """One Messages API call, with typed errors mapped to LLMError."""
        model = kwargs["model"]
        use_fallbacks = self.settings.use_refusal_fallback(model) and not self._fallbacks_rejected
        try:
            if use_fallbacks:
                try:
                    return self.client.beta.messages.create(
                        betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                    )
                except anthropic.BadRequestError as exc:
                    text = str(exc).lower()
                    if "fallback" not in text and "beta" not in text:
                        raise
                    # This endpoint/gateway does not accept the refusal-fallback opt-in;
                    # carry on without it (refusals are still handled below).
                    log.warning("refusal fallback not accepted (%s); continuing without it", exc)
                    self._fallbacks_rejected = True
            return self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("invalid_api_key", "The model API key was rejected.", 401) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(
                "invalid_api_key", f"The API key is not permitted to use {model}.", 401
            ) from exc
        except anthropic.NotFoundError as exc:
            raise LLMError("llm_error", f"Model '{model}' was not found.", 502) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("rate_limited", "The model API rate limit was hit; try again shortly.", 429) from exc
        except anthropic.BadRequestError as exc:
            raise LLMError("llm_error", f"The model API rejected the request: {exc.message}", 502) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError("llm_error", f"The model API returned an error ({exc.status_code}).", 502) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("llm_error", "Could not reach the model API.", 502) from exc
        except TypeError as exc:
            if "authentication" in str(exc).lower():
                raise LLMError("missing_api_key", "No model API key is configured.", 401) from exc
            raise

    def _output_config(self, effort: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if effort and effort != "none":
            cfg["effort"] = effort
        if schema is not None:
            cfg["format"] = {"type": "json_schema", "schema": schema}
        return cfg

    def _structured(
        self, purpose: str, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8000
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        model = self.settings.model_understand
        failure = LLMRefusal(f"{purpose}: no output", kind="unusable")
        for _attempt in range(2):  # one retry when the output is truncated or does not parse
            started = time.perf_counter()
            resp = self._create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config=self._output_config(self.settings.effort_understand, schema),
            )
            meta = {
                "call": purpose,
                "model": getattr(resp, "model", model),
                "ms": int((time.perf_counter() - started) * 1000),
                "stop_reason": resp.stop_reason,
                **_usage(resp),
            }
            if resp.stop_reason == "refusal":
                raise LLMRefusal(purpose)
            try:
                data = json.loads(_text(resp))
            except json.JSONDecodeError:  # e.g. truncated at max_tokens
                failure = LLMRefusal(
                    f"{purpose}: unparseable output ({resp.stop_reason})", kind="unusable"
                )
                continue
            if isinstance(data, dict):
                return data, meta
            failure = LLMRefusal(f"{purpose}: output is not an object", kind="unusable")
        raise failure

    # -- operations ------------------------------------------------------------
    def understand(
        self, state: SessionState, user_text: str, settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._structured(
            "understand",
            prompts.UNDERSTAND_SYSTEM,
            prompts.understand_user_prompt(state, user_text, settings),
            UNDERSTANDING_SCHEMA,
        )

    def resolve(
        self, state: SessionState, claims: list[dict[str, Any]], settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return self._structured(
                "resolve",
                prompts.RESOLVE_SYSTEM,
                prompts.resolve_user_prompt(state, claims, settings),
                RESOLUTION_SCHEMA,
            )
        except LLMRefusal as exc:
            # No decision is not the same as "ambiguous": fail the turn (nothing is recorded,
            # the caller's message can be resent) rather than guess or wipe the open question.
            raise LLMError(
                "llm_error",
                "The model could not complete this step; nothing was recorded. Please send the "
                "message again.",
                502,
            ) from exc

    def summarize(
        self, state: SessionState, records: list[dict[str, Any]], settings: Settings
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return self._structured(
                "summarize",
                prompts.SUMMARIZE_SYSTEM,
                prompts.summarize_user_prompt(state, records, settings),
                SUMMARY_SCHEMA,
            )
        except LLMRefusal:
            return {}, {}  # the harness falls back to a summary built from the records

    def respond(
        self,
        state: SessionState,
        directive: Any,  # unused here; the mock backend phrases from it
        control_block: str,
        tool_specs: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        settings: Settings,
    ) -> RespondResult:
        model = settings.model_respond
        # The system prompt and tool list stay byte-identical for every request in the
        # tool loop below, so thinking blocks returned mid-loop remain valid.
        system = [
            {"type": "text", "text": prompts.RESPOND_SYSTEM, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": control_block},
        ]
        messages: list[dict[str, Any]] = prompts.conversation_messages(state, settings.history_window)
        kwargs: dict[str, Any] = {"model": model, "max_tokens": 16000, "system": system}
        output_config = self._output_config(settings.effort_respond)
        if output_config:
            kwargs["output_config"] = output_config
        if tool_specs:
            kwargs["tools"] = tool_specs
        calls: list[dict[str, Any]] = []
        started = time.perf_counter()
        usage = {"input_tokens": 0, "output_tokens": 0}
        resp = None
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            call_kwargs = dict(kwargs)
            if tool_specs and round_no == MAX_TOOL_ROUNDS:
                # Out of tool rounds: the model has to answer from what it already has.
                call_kwargs["tool_choice"] = {"type": "none"}
            resp = self._create(messages=messages, **call_kwargs)
            for key in usage:
                usage[key] += getattr(resp.usage, key, 0) or 0
            if resp.stop_reason == "refusal":
                raise LLMRefusal("respond")
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason == "tool_use" and tool_uses:
                # Thinking blocks must go back unmodified, so append the content as returned.
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in tool_uses:
                    args = dict(block.input) if isinstance(block.input, dict) else {}
                    try:
                        output = tool_executor(block.name, args)
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(output),
                            }
                        )
                        calls.append({"name": block.name, "args": args, "ok": True})
                    except ToolError as exc:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Error: {exc}",
                                "is_error": True,
                            }
                        )
                        calls.append({"name": block.name, "args": args, "ok": False, "error": str(exc)})
                messages.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        # A preamble before an unanswered tool call, or a reply cut off at the token limit, is
        # not an answer; the caller gets the template wording instead of half a sentence.
        if resp is None or resp.stop_reason in {"tool_use", "max_tokens", "pause_turn"}:
            reason = getattr(resp, "stop_reason", "no response")
            raise LLMRefusal(f"respond: no complete answer ({reason})", kind="unusable")
        text = _text(resp)
        if not text:
            raise LLMRefusal("respond: empty reply", kind="unusable")
        return RespondResult(
            text=text,
            tool_calls=calls,
            meta={
                "call": "respond",
                "model": getattr(resp, "model", model),
                "ms": int((time.perf_counter() - started) * 1000),
                "stop_reason": resp.stop_reason,
                **usage,
            },
        )
