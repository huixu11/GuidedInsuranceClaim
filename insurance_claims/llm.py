"""Claude backend (Anthropic SDK).

Four operations, all on the Messages API:

* understand / resolve / summarize — structured outputs (`output_config.format`),
  so the result always parses against the schema the harness expects.
* respond — free text, with an optional tool loop restricted to the tools the
  SOP allows in the current phase.

When the model service fails (see `AnthropicBackend._create`):

* One attempt of a call is cut off after `model_timeout_seconds`; the SDK retries a failed
  attempt `model_max_retries` times (connection errors, 429, 5xx).
* No call starts later than the turn's budget (`turn_budget`), so a turn has an upper bound and
  never outlives the hold on its conversation.
* A circuit breaker shared by every key in the process refuses calls at once (503
  `model_unavailable`) while the service is not answering, instead of letting every turn wait
  for its own timeout while it holds a turn slot.

What the caller sees depends on where the turn was (agent.py): before the SOP engine has decided,
the turn fails and nothing is recorded; after it, the fixed template wording is sent.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


CONNECT_TIMEOUT_SECONDS = 5.0


class LLMError(Exception):
    """A model call failed in a way the user should hear about (auth, quota, outage)."""

    def __init__(self, code: str, message: str, status: int = 502, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after  # seconds, for a Retry-After header


class _Unanswered(LLMError):
    """The model service did not answer: no connection, a timeout, a 5xx."""


class CircuitBreaker:
    """Stops sending requests to a model service that is not answering.

    Closed: calls go through, and `threshold` failures in a row open the breaker. Open: calls are
    refused at once for `open_seconds`. After that, half-open: one call at a time is let through
    as a probe; its success closes the breaker, its failure opens it again.

    A failure is the service not answering: no connection, a timeout, a 5xx. An answer of any
    other kind, a rejected key or a rate limit included, shows that the service is up and counts
    as a success, so one caller's bad key cannot close the model for everyone else.
    """

    def __init__(
        self,
        threshold: int,
        open_seconds: float,
        probe_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = max(0, int(threshold))  # 0: the breaker never opens
        self.open_seconds = float(open_seconds)
        self.probe_seconds = float(probe_seconds)  # a probe that never reports is given up after this
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_started: float | None = None
        self.opened_total = 0
        self.refused_total = 0

    def admit(self) -> float:
        """0.0 if a call may go ahead now, else the seconds until one may be tried."""
        if not self.threshold:
            return 0.0
        with self._lock:
            if self._opened_at is None:
                return 0.0
            now = self._clock()
            wait = self._opened_at + self.open_seconds - now
            if wait <= 0:
                if self._probe_started is None or now - self._probe_started >= self.probe_seconds:
                    self._probe_started = now  # this call is the probe
                    return 0.0
                wait = 1.0  # a probe is under way; its result is due shortly
            self.refused_total += 1
            return wait

    def record(self, answered: bool) -> None:
        """The outcome of a call that was admitted: the service answered, or it did not."""
        if not self.threshold:
            return
        with self._lock:
            if answered:
                self._failures, self._opened_at, self._probe_started = 0, None, None
                return
            self._failures += 1
            if self._opened_at is None:
                if self._failures >= self.threshold:
                    self._opened_at = self._clock()
                    self.opened_total += 1
            elif self._probe_started is not None:  # the probe failed: open again
                self._opened_at, self._probe_started = self._clock(), None
            # Otherwise a call admitted before the breaker opened has failed too: nothing changes.

    def abandon(self) -> None:
        """A call ended without telling anything about the service (a programming error): if it
        was the probe, let another one through."""
        with self._lock:
            self._probe_started = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._opened_at is None:
                state, retry_in = "closed", 0.0
            else:
                retry_in = max(0.0, self._opened_at + self.open_seconds - self._clock())
                state = "open" if retry_in > 0 else "half_open"
            return {
                "state": state,
                "enabled": bool(self.threshold),
                "consecutive_failures": self._failures,
                "opens_after_failures": self.threshold,
                "open_seconds": self.open_seconds,
                "retry_in_seconds": round(retry_in, 1),
                "opened_since_start": self.opened_total,
                "calls_refused_since_start": self.refused_total,
            }


# One breaker for the process: whether the service answers does not depend on whose key is used.
_shared_breaker: CircuitBreaker | None = None
_shared_lock = threading.Lock()


def shared_breaker(settings: Settings) -> CircuitBreaker:
    global _shared_breaker
    with _shared_lock:
        if _shared_breaker is None:
            _shared_breaker = CircuitBreaker(
                settings.model_breaker_failures,
                settings.model_breaker_seconds,
                probe_seconds=settings.model_timeout_seconds * (1 + settings.model_max_retries),
            )
        return _shared_breaker


def reset_model_health() -> None:
    """Forget what the process has learnt about the model service (tests)."""
    global _shared_breaker
    with _shared_lock:
        _shared_breaker = None


def model_health() -> dict[str, Any]:
    """The state of the process's circuit breaker, for /api/stats. No keys, no content."""
    with _shared_lock:
        breaker = _shared_breaker
    if breaker is None:
        return {"state": "closed", "note": "this process has not made a model call yet"}
    return breaker.snapshot()


# The time by which the turn running in this thread must have started its last model call.
_turn_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("sop_turn_deadline", default=None)


@contextmanager
def turn_budget(seconds: float) -> Iterator[None]:
    """Bound the model calls made inside the block (one turn, on its own thread): each attempt
    gets at most the time that is left, and once none is left no further call is started."""
    token = _turn_deadline.set(time.monotonic() + max(0.0, float(seconds)))
    try:
        yield
    finally:
        _turn_deadline.reset(token)


def _retry_after(exc: Any, default: int | None = None) -> int | None:
    """The service's own Retry-After (whole seconds, 1 to 60), if it sent one."""
    try:
        value = float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return max(1, min(60, math.ceil(value)))


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
        self.timeout = float(settings.model_timeout_seconds)
        kwargs: dict[str, Any] = {
            "max_retries": settings.model_max_retries,
            "timeout": anthropic.Timeout(self.timeout, connect=min(CONNECT_TIMEOUT_SECONDS, self.timeout)),
        }
        if api_key:
            kwargs["api_key"] = api_key
        self.client = anthropic.Anthropic(**kwargs)
        self.breaker = shared_breaker(settings)
        self._fallbacks_rejected = False

    # -- transport -----------------------------------------------------------
    def _attempt_timeout(self) -> Any:
        """The timeout for a call made now: the client's own outside a turn budget; inside one,
        no more than the time the turn has left (LLMError once none is left)."""
        deadline = _turn_deadline.get()
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left < 1.0:
            raise LLMError(
                "turn_timeout",
                "The model took too long: the time allowed for answering one message ran out.",
                504,
            )
        return anthropic.Timeout(min(self.timeout, left), connect=min(CONNECT_TIMEOUT_SECONDS, left))

    def _create(self, **kwargs: Any) -> Any:
        """One Messages API call: bounded by the turn's budget, refused at once while the
        circuit breaker is open, and with typed errors mapped to LLMError."""
        timeout = self._attempt_timeout()
        if timeout is not None:
            kwargs["timeout"] = timeout
        wait = self.breaker.admit()
        if wait > 0:
            raise LLMError(
                "model_unavailable",
                "The model service is not answering right now; please try again shortly.",
                503,
                retry_after=math.ceil(wait),
            )
        answered: bool | None = None  # None: the call told us nothing about the service
        try:
            response = self._call(**kwargs)
            answered = True
            return response
        except LLMError as exc:
            answered = not isinstance(exc, _Unanswered)
            raise
        finally:
            if answered is None:
                self.breaker.abandon()
            else:
                self.breaker.record(answered)

    def _call(self, **kwargs: Any) -> Any:
        """The call itself. `_Unanswered` (an LLMError) when the service did not answer; any
        other LLMError means it did."""
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
            raise LLMError(
                "rate_limited", "The model API rate limit was hit; try again shortly.", 429, _retry_after(exc)
            ) from exc
        except anthropic.BadRequestError as exc:
            raise LLMError("llm_error", f"The model API rejected the request: {exc.message}", 502) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code < 500:
                raise LLMError("llm_error", f"The model API returned an error ({exc.status_code}).", 502) from exc
            if exc.status_code in (503, 529):  # overloaded; the SDK has already retried
                raise _Unanswered(
                    "model_overloaded",
                    "The model service is overloaded; please try again shortly.",
                    503,
                    _retry_after(exc, 5),
                ) from exc
            raise _Unanswered("llm_error", f"The model API returned an error ({exc.status_code}).", 502) from exc
        except anthropic.APITimeoutError as exc:
            raise _Unanswered("llm_error", "The model API did not answer in time.", 504) from exc
        except anthropic.APIConnectionError as exc:
            raise _Unanswered("llm_error", "Could not reach the model API.", 502) from exc
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
