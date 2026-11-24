"""HTTP API + static test UI.

Serving model (README, "Scaling and limits"): a turn waits for one of SOP_MAX_CONCURRENT_TURNS
slots (briefly; then 503 server_busy with Retry-After), then runs on a worker thread of its own
pool while it holds the conversation through the session store (another message for the same
conversation waits briefly, then 409 conversation_busy). Everything else (health, config, stats,
static files, reads) never needs a turn slot. Bodies over 16 KB are refused (413) before they
are parsed, and an optional per-client rate limit answers 429. Every refusal uses the error
envelope and leaves the conversation exactly as it was.

Recovering from failures:

- A message may carry a `message_id` chosen by the client. A retry with the same id never runs
  the turn twice: once the first attempt has committed, the retry is answered from the transcript
  (`"replayed": true`), on whichever instance it reaches.
- What a turn decided to send (the summary e-mail, the handoff to a human) is queued in the
  store with the turn's commit and delivered by a dispatcher (dispatch.py), one per process.
  Reading a conversation shows each one's delivery ({status: queued | sent | failed, attempts,
  sent_at}); a turn's reply shows it for what that turn queued. GET /api/sessions/{id}/audit
  lists the audit events and the queued effects, without their content.
- /healthz is liveness; /readyz says whether this instance can reach its store. On SIGTERM the
  instance stops accepting, lets turns in flight finish (SOP_SHUTDOWN_GRACE_SECONDS), then stops
  its dispatcher and closes the store; a turn still running after that is not committed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import statistics
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from typing import Any

import anyio
from anyio.lowlevel import RunVar
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import llm, sop
from .agent import Agent, ConversationClosed
from .config import AGENT_NAME, APP_NAME, STATIC_DIR, Settings, load_settings, server_has_credentials
from .data import Repository
from .dispatch import QUEUED, Dispatcher, delivery_of, turn_effects, with_delivery
from .llm import AnthropicBackend, LLMError
from .mock_llm import MockBackend
from .state import SessionState
from .store import (
    ConversationBusy,
    SessionNotFound,
    SessionStore,
    StoreError,
    StoreUnavailable,
    open_store,
)

log = logging.getLogger("sop.server")
turn_log = logging.getLogger("sop.turn")

settings = load_settings()
repo = Repository.load()
agent = Agent(repo, settings)

MAX_BODY_BYTES = 16 * 1024  # request bodies; a 4,000-character message is at most ~16,000 bytes of UTF-8
MAX_MESSAGE_CHARS = 4000
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # ids are uuid4 hex; anything else cannot exist
MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")  # chosen by the client, one per message (not per attempt)
READY_TIMEOUT_SECONDS = 2.0  # /readyz answers 503 when the store does not answer within this

# Suggested caller messages for the test UI, tagged with where in the SOP they make sense.
#   phases      The phases the suggestion fits.
#   happy_path  The one that carries the demo forward from there; the UI lists it first and
#               marks it as the suggested next step. The others are detours worth trying.
#   then        Optional, on a happy-path one: once it has been sent in the current visit to
#               the phase, the suggestion with this label is marked as the next step instead.
#   when        Optional: only at one point of those phases, named after what the agent is
#               waiting for - "consent" (a representative waits for the policyholder's
#               consent), "email_offer" (send or skip the email summary?) or "wrapped_up" (the
#               conversation is completed; a follow-up re-opens it). Suggestions without it
#               are for the phase's other turns, and are also shown at a point that has no
#               suggestions of its own. Nothing is suggested once a call is escalated.
# The order is the display order. The UI's suggestion row shows the marked next step and then
# the first three other suggestions for that point, in list order; every suggestion is listed
# by phase under "More". So the detours that matter most at each point come first.
# After VERIFY_ID the texts name no one, so they suit any of the fixture callers.
SAMPLE_PROMPTS: list[dict[str, Any]] = [
    # -- in the suggestion row ----------------------------------------------------------
    # VERIFY_ID: the demo, then a frustrated caller, a partial answer and an off-topic question.
    {
        "label": "Demo test case",
        "text": (
            "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my "
            "denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
        ),
        "phases": ["VERIFY_ID"],
        "happy_path": True,
    },
    {
        "label": "Frustrated caller",
        "text": "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
        "phases": ["VERIFY_ID"],
    },
    {
        "label": "Partial ID",
        "text": "My name is Margaret Chen and my date of birth is March 15, 1985.",
        "phases": ["VERIFY_ID"],
    },
    {
        "label": "Consent approved",
        "text": "It's been approved now. Can you check again?",
        "phases": ["VERIFY_ID"],
        "when": ["consent"],
        "happy_path": True,
    },
    # RESOLVE_INTENT: name the claim, or go off topic, or ask for a human (both further down).
    {
        "label": "Name the claim",
        "text": "It's about the denied healthcare claim.",
        "phases": ["RESOLVE_INTENT"],
        "happy_path": True,
    },
    # PROCESS_CASE: a documents question, then why it was denied, frustration, off topic.
    {
        "label": "Document follow-up",
        "text": "How do I submit the missing documents?",
        "phases": ["PROCESS_CASE"],
        "happy_path": True,
        "then": "Wrap up",
    },
    {"label": "Why denied", "text": "Why was the claim denied?", "phases": ["PROCESS_CASE"]},
    {
        "label": "Frustrated caller",
        "text": (
            "This is ridiculous. I've been waiting for weeks and nobody can tell me what's going on "
            "with my claim."
        ),
        "phases": ["PROCESS_CASE"],
    },
    # Any phase before the wrap-up.
    {
        "label": "Out of scope",
        "text": "What is RL?",
        "phases": ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE"],
    },
    {
        "label": "Ask for a human",
        "text": "I'd like to speak to a human representative, please.",
        "phases": ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE"],
    },
    # POST_PROCESS: send or skip the summary, another address, one more question.
    {
        "label": "Send the summary",
        "text": "Yes, please send it.",
        "phases": ["POST_PROCESS"],
        "when": ["email_offer"],
        "happy_path": True,
    },
    {
        "label": "Skip the summary",
        "text": "No thanks, skip the email.",
        "phases": ["POST_PROCESS"],
        "when": ["email_offer"],
    },
    {
        "label": "Work address instead",
        "text": "Please send it to my work address instead.",
        "phases": ["POST_PROCESS"],
        "when": ["email_offer"],
    },
    {
        "label": "One more question",
        "text": "Actually, one more question: when is the appeal deadline?",
        "phases": ["POST_PROCESS"],
        "when": ["email_offer", "wrapped_up"],
    },
    # -- under "More" -------------------------------------------------------------------
    {
        "label": "Intent before ID",
        "text": "Hi, I'm calling about my denied healthcare claim from January.",
        "phases": ["VERIFY_ID"],
    },
    {
        "label": "Phone + email",
        "text": "My phone is 650-521-2836 and my email is margaret@email.com.",
        "phases": ["VERIFY_ID"],
    },
    {
        "label": "Won't share SSN",
        "text": "I'm Margaret Chen, but I'm not comfortable giving my SSN. Is there something else I can use?",
        "phases": ["VERIFY_ID"],
    },
    {
        "label": "Representative",
        "text": (
            "Hi, I'm David Chen, calling on behalf of my mother Margaret Chen about her denied claim. "
            "Her date of birth is 1985-03-15 and her phone is 650-521-2836."
        ),
        "phases": ["VERIFY_ID"],
    },
    {"label": "Different claim", "text": "Can you also check on my auto claim?", "phases": ["PROCESS_CASE"]},
    # Marked as the next step once "Document follow-up" has been sent (its `then`).
    {"label": "Wrap up", "text": "That's all I needed, thanks.", "phases": ["PROCESS_CASE"]},
]


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retry_after = retry_after  # seconds, sent as Retry-After


def error_response(
    status: int, code: str, message: str, retry_after: int | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    extra = dict(headers or {})
    if retry_after is not None:
        extra["Retry-After"] = str(max(1, int(retry_after)))
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=extra)


# ---------------------------------------------------------------------------
# Serving machinery: turn slots, rate limit, counters
# ---------------------------------------------------------------------------
class _SlotWaiter:
    __slots__ = ("loop", "future", "granted")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.future: asyncio.Future[None] = loop.create_future()
        self.granted = False


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class TurnSlots:
    """At most `size` turns at once. A turn waits for a slot without holding a thread, first come
    first served, for a bounded time. Thread-safe and usable from any event loop (uvicorn runs
    one per process; the test client runs one per request)."""

    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self._lock = threading.Lock()
        self._used = 0
        self._waiters: deque[_SlotWaiter] = deque()

    @property
    def in_use(self) -> int:
        return self._used

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._used < self.size and not self._waiters:
                self._used += 1
                return True
            if timeout <= 0:
                return False
            waiter = _SlotWaiter(loop)
            self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.future, timeout)
            return True
        except TimeoutError:
            with self._lock:
                if waiter.granted:  # handed over just as the wait ran out: keep it
                    return True
                self._waiters.remove(waiter)
            return False
        except BaseException:  # cancelled while waiting
            with self._lock:
                granted = waiter.granted
                if not granted:
                    self._waiters.remove(waiter)
            if granted:
                self.release()
            raise

    def release(self) -> None:
        with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                try:
                    waiter.loop.call_soon_threadsafe(_wake, waiter.future)
                except RuntimeError:  # its event loop is gone; so is the waiter
                    continue
                waiter.granted = True  # the slot passes straight to the longest waiter
                return
            self._used -= 1


class RateLimiter:
    """Token buckets per client and kind: `per_minute` turns, and a quarter of that (at least
    one) for new conversations. In memory, so with several workers or instances each keeps its
    own buckets; a gateway in front is the place for a global limit."""

    MAX_CLIENTS = 10_000  # buckets kept; the least recently seen client is forgotten first

    def __init__(self, per_minute: int) -> None:
        self.budgets = {"turn": float(per_minute), "create": float(max(1, per_minute // 4))}
        self._lock = threading.Lock()
        self._buckets: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    def take(self, client: str, kind: str, now: float | None = None) -> float:
        """0.0 if the request may go ahead, else the seconds until it could."""
        capacity = self.budgets[kind]
        rate = capacity / 60.0
        now = time.monotonic() if now is None else now
        key = (kind, client)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = [capacity, now]
                if len(self._buckets) > self.MAX_CLIENTS:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
                bucket[0] = min(capacity, bucket[0] + (now - bucket[1]) * rate)
                bucket[1] = now
            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                return 0.0
            return (1.0 - bucket[0]) / rate


COUNTERS = (
    "conversations_created",
    "turns_answered",
    "refused_busy_503",
    "conflicts_409",
    "rate_limited_429",
    "too_large_413",
    "model_errors",
    "closed_at_message_limit_409",
    "replayed_messages",  # a retried message answered from the transcript instead of run again
)


class Stats:
    """Counters since start and the latency of the last 1,000 answered turns (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(COUNTERS, 0)
        self._latency: deque[float] = deque(maxlen=1000)
        self.started = time.monotonic()

    def count(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def turn_answered(self, seconds: float) -> None:
        with self._lock:
            self._counts["turns_answered"] += 1
            self._latency.append(seconds)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def latency(self) -> dict[str, Any]:
        with self._lock:
            window = sorted(self._latency)
        if not window:
            return {"turns": 0, "median_ms": None, "p95_ms": None}
        return {
            "turns": len(window),
            "median_ms": round(statistics.median(window) * 1000, 1),
            "p95_ms": round(window[max(0, math.ceil(0.95 * len(window)) - 1)] * 1000, 1),
        }

    def retry_after(self) -> int:
        """About one turn's time: when a busy server is likely to have a free slot again."""
        with self._lock:
            window = list(self._latency)
        if not window:
            return 1
        return max(1, min(30, math.ceil(statistics.median(window))))


class Runtime:
    """What serving needs besides the engine: the store, the turn slots, the rate limiter, the
    counters and the dispatcher that delivers queued effects (started by the app's lifespan).
    Built once per process from the settings; tests swap in their own."""

    def __init__(self, cfg: Settings, store: SessionStore | None = None) -> None:
        self.settings = cfg
        self.store = store if store is not None else open_store(cfg)
        self.slots = TurnSlots(cfg.max_concurrent_turns)
        self.limiter = RateLimiter(cfg.rate_limit_per_minute) if cfg.rate_limit_per_minute > 0 else None
        self.stats = Stats()
        # Built now, so that a misconfigured transport stops the start; started by the lifespan.
        self.dispatcher = Dispatcher(self.store, repo, cfg) if cfg.dispatch else None
        # Set once the shutdown is past its grace period: a turn still running is not committed.
        self.closing = False

    def describe(self) -> str:
        budget = getattr(self.store, "max_bytes", 0)
        d = self.dispatcher
        return (
            f"{self.store.kind} session store"
            + (" (shared)" if self.store.shared else " (this process only)")
            + f", {self.slots.size} turn slots, at most {self.settings.max_turns_per_conversation} "
            f"messages per conversation, {self.store.max_sessions} conversations"
            + (f" or {budget // (1024 * 1024)} MB of state" if budget else "")
            + ", rate limit "
            + (f"{self.settings.rate_limit_per_minute}/min per client" if self.limiter else "off")
            + ", delivery "
            + (f"by this process (e-mail: {d.email.name}, handoff: {d.handoff.name})" if d else "off (SOP_DISPATCH=off)")
        )


runtime = Runtime(settings)

# Turns run on worker threads of their own, sized to the turn slots, so they can never use up
# the default pool that serves reads, session creation and static files (one per event loop).
# Readiness checks get two threads of their own, so a store that hangs cannot pile them up.
_turn_threads: RunVar[anyio.CapacityLimiter] = RunVar("sop_turn_threads")
_ping_threads: RunVar[anyio.CapacityLimiter] = RunVar("sop_ping_threads")


def _thread_limiter(var: RunVar[anyio.CapacityLimiter], size: int) -> anyio.CapacityLimiter:
    try:
        limiter = var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(size)
        var.set(limiter)
    if limiter.total_tokens != size:
        limiter.total_tokens = size
    return limiter


def _turn_thread_limiter(size: int) -> anyio.CapacityLimiter:
    return _thread_limiter(_turn_threads, size)


_backends: dict[str, Any] = {}
_backends_lock = threading.Lock()


def get_backend(api_key: str | None) -> Any:
    """The backend that answers one request.

    mock: always the offline double. anthropic: always the model, with the caller's key or else
    the server's (neither: missing_api_key). auto: the model when a key is available from either
    place, otherwise the offline double, so a hosted demo works without a key and each tester
    who brings one gets the model.
    """
    key = (api_key or "").strip() or None
    offline = settings.llm_mode == "mock" or (
        settings.llm_mode == "auto" and key is None and not server_has_credentials()
    )
    if offline:
        with _backends_lock:
            backend = _backends.get("mock")
            if backend is None:
                backend = _backends["mock"] = MockBackend(settings.mock_latency_ms)
            return backend
    cache_key = "server" if key is None else hashlib.sha256(key.encode()).hexdigest()
    with _backends_lock:
        backend = _backends.get(cache_key)
        if backend is None:
            try:
                backend = AnthropicBackend(settings, key)
            except LLMError as exc:
                raise ApiError(exc.code, exc.message, exc.status, exc.retry_after) from exc
            if len(_backends) > 64:
                _backends.clear()
            _backends[cache_key] = backend
        return backend


# ---------------------------------------------------------------------------
# App, request-size limit and error envelope
# ---------------------------------------------------------------------------
class PayloadTooLarge(StarletteHTTPException):
    def __init__(self) -> None:
        super().__init__(413, f"The request body is larger than {MAX_BODY_BYTES // 1024} KB.")


class BodySizeLimit:
    """Refuses request bodies over `max_bytes` with 413 before anything parses them: at once by
    the declared Content-Length, and by counting the bytes as they arrive (a chunked body
    declares no length)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared: int | None = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
        if declared is not None and (declared < 0 or declared > self.max_bytes):
            if declared < 0:
                response = error_response(400, "bad_request", "The Content-Length header is not valid.")
            else:
                runtime.stats.count("too_large_413")
                response = error_response(413, "payload_too_large", PayloadTooLarge().detail)
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise PayloadTooLarge()  # FastAPI passes HTTP exceptions through to the handler
            return message

        await self.app(scope, limited_receive, send)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> Any:
    rt = runtime
    rt.closing = False
    log.info("serving with %s", rt.describe())
    if rt.dispatcher is not None:
        rt.dispatcher.start()
    try:
        yield
    finally:
        # Shutdown: uvicorn has stopped accepting and waited for the requests in flight (at most
        # SOP_SHUTDOWN_GRACE_SECONDS). A turn still running now is not committed (_run_turn); its
        # hold on the conversation is released, or lapses within one lease, and the client's
        # retry is answered by another instance.
        rt.closing = True
        if rt.dispatcher is not None:
            await anyio.to_thread.run_sync(rt.dispatcher.stop)
        rt.store.close()


app = FastAPI(title=APP_NAME, docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)
app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.message, exc.retry_after)


_HTTP_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Unknown routes and methods, and bodies found too large while reading them, use the same
    error envelope as everything else."""
    if exc.status_code == 413:
        runtime.stats.count("too_large_413")
    code = _HTTP_CODES.get(exc.status_code, "bad_request")
    return error_response(exc.status_code, code, str(exc.detail), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(400, "bad_request", "The request body is not valid.")


@app.exception_handler(Exception)
async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled error", exc_info=exc)
    return error_response(500, "internal_error", "Something went wrong on the server.")


class NewSession(BaseModel):
    consent_scenario: str = "default"


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    # Optional, chosen by the client once per message and sent again with every retry of it.
    message_id: str | None = None


def _client_of(request: Request) -> str:
    """Who a rate limit applies to: the socket peer, or with SOP_TRUST_PROXY=1 the last hop of
    X-Forwarded-For (the address our own proxy saw; earlier hops are whatever the client sent)."""
    if runtime.settings.trust_proxy:
        hops = [h.strip() for h in ",".join(request.headers.getlist("x-forwarded-for")).split(",")]
        hops = [h for h in hops if h]
        if hops:
            return hops[-1][:100]
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, kind: str) -> None:
    limiter = runtime.limiter
    if limiter is None:
        return
    wait = limiter.take(_client_of(request), kind)
    if wait > 0:
        runtime.stats.count("rate_limited_429")
        what = "messages" if kind == "turn" else "new conversations"
        raise ApiError("rate_limited", f"Too many {what} from this client; please wait a moment.", 429,
                       retry_after=math.ceil(wait))


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID.fullmatch(session_id):
        raise ApiError("session_not_found", "This conversation no longer exists on the server.", 404)


def _store_unavailable() -> ApiError:
    runtime.stats.count("refused_busy_503")
    return ApiError("server_busy", "The session store is busy; please try again in a moment.", 503, retry_after=1)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process answers. Says nothing about the store (see /readyz)."""
    return {"status": "ok"}


_ready_state = {"ready": True}  # the last answer of /readyz, so only a change is logged


@app.get("/readyz", response_model=None)
async def readyz() -> dict[str, str] | JSONResponse:
    """Readiness: this instance can serve conversations, i.e. its store answers (within ~2 s)
    and it is not shutting down. 503 not_ready otherwise, for a load balancer to route around it."""
    rt = runtime
    reason = "shutting_down" if rt.closing else ""
    if not reason:
        try:
            with anyio.fail_after(READY_TIMEOUT_SECONDS):
                await anyio.to_thread.run_sync(
                    rt.store.ping, abandon_on_cancel=True, limiter=_thread_limiter(_ping_threads, 2)
                )
        except TimeoutError:
            reason = "store_timeout"
        except StoreError as exc:
            reason = f"store_unavailable ({type(exc).__name__})"
    ready = not reason
    if ready != _ready_state["ready"]:
        _ready_state["ready"] = ready
        if ready:
            log.info("ready again: the session store answers")
        else:
            log.warning("not ready: %s", reason)
    if ready:
        return {"status": "ready"}
    message = (
        "This instance is shutting down."
        if rt.closing
        else "This instance cannot reach its session store right now."
    )
    return error_response(503, "not_ready", message, retry_after=1)


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "app_name": APP_NAME,
        "agent_name": AGENT_NAME,
        "llm_mode": settings.llm_mode,
        "has_server_key": server_has_credentials(),
        "models": {"respond": settings.model_respond, "understand": settings.model_understand},
        "demo_today": settings.demo_today.isoformat(),
        "consent_scenarios": list(repo.consent_scenarios),
        "phases": sop.phases_for_ui(),
        "limits": {
            "required_id_matches": settings.required_id_matches,
            "max_failed_attempts": settings.max_failed_attempts,
            "out_of_scope_limit": settings.out_of_scope_limit,
            "max_persuasion_attempts": settings.max_persuasion_attempts,
            "max_turns_per_conversation": agent.settings.max_turns_per_conversation,  # the engine enforces it
            "max_message_chars": MAX_MESSAGE_CHARS,
        },
        "sample_prompts": SAMPLE_PROMPTS,
        "fixtures_exposed": settings.expose_fixtures,
    }


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    """Load and health of this process, for operators. No conversation content or identifiers."""
    rt = runtime
    try:
        store = await anyio.to_thread.run_sync(rt.store.stats)
    except StoreError:
        store = {"kind": rt.store.kind, "unavailable": True}
    pid = os.getpid()
    dispatcher = (
        rt.dispatcher.snapshot()
        if rt.dispatcher is not None
        else {"enabled": False, "running": False, "note": "SOP_DISPATCH=off: another process delivers"}
    )
    return {
        "uptime_seconds": round(time.monotonic() - rt.stats.started, 1),
        "process": {"pid": pid},
        "store": store,
        "model": llm.model_health(),
        "dispatcher": dispatcher,
        "turn_slots": {"in_use": rt.slots.in_use, "max": rt.slots.size, "waiting": rt.slots.waiting},
        "counters": {
            "scope": f"this process (pid {pid}) since it started; each worker or instance counts its own",
            **rt.stats.counts(),
        },
        "turn_latency_ms": {
            "scope": "last 1,000 answered turns in this process, from a free turn slot to the commit",
            **rt.stats.latency(),
        },
        "limits": {
            "max_concurrent_turns": rt.slots.size,
            "turn_slot_wait_seconds": rt.settings.turn_slot_wait_seconds,
            "conversation_wait_seconds": rt.settings.conversation_wait_seconds,
            "max_sessions": rt.store.max_sessions,
            "max_turns_per_conversation": agent.settings.max_turns_per_conversation,
            "max_body_bytes": MAX_BODY_BYTES,
            "rate_limit_per_minute": rt.settings.rate_limit_per_minute or None,
        },
    }


@app.get("/api/fixtures")
async def get_fixtures() -> dict[str, Any]:
    """Demo aid: the test identities and their claims, so an evaluator knows what to type."""
    if not settings.expose_fixtures:
        raise ApiError("not_found", "Fixtures are not exposed on this deployment.", 404)
    return {
        "policyholders": [
            {
                "party_id": p.party_id,
                "name": p.name,
                "policy_number": p.policy_number,
                "dob": p.dob,
                "id_type": p.id_type,
                "id_last4": p.id_last4,
                "phone": p.phone,
                "email": p.email,
                "claims": [repo.claim_index_entry(c) for c in repo.claims_for(p.party_id)],
            }
            for p in repo.policyholders
        ],
        "representatives": repo.representatives,
    }


@app.post("/api/sessions")
def create_session(request: Request, body: NewSession | None = None) -> dict[str, Any]:
    _rate_limit(request, "create")
    state = agent.new_session((body.consent_scenario if body else "default"))
    try:
        runtime.store.create(state)
    except StoreUnavailable as exc:
        raise _store_unavailable() from exc
    runtime.stats.count("conversations_created")
    return {
        "session_id": state.session_id,
        "messages": state.messages,
        "state": state.to_view(settings),
    }


def _deliveries(store: SessionStore, session_id: str, state: SessionState) -> dict[str, dict[str, Any]]:
    """Effect key -> delivery ({status, attempts, sent_at}) of what the conversation queued;
    the store is asked only if there is something to show."""
    if not state.outbox and state.escalation is None:
        return {}
    return {str(effect["key"]): delivery_of(effect) for effect in store.effects(session_id)}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    _check_session_id(session_id)
    store = runtime.store
    try:
        state = store.read(session_id)
        deliveries = _deliveries(store, session_id, state)
    except SessionNotFound as exc:
        raise ApiError("session_not_found", "This conversation no longer exists on the server.", 404) from exc
    except StoreUnavailable as exc:
        raise _store_unavailable() from exc
    return {
        "session_id": state.session_id,
        "messages": state.messages,
        "state": with_delivery(state.to_view(settings), deliveries),
        "traces": state.traces,
    }


@app.get("/api/sessions/{session_id}/audit")
def get_audit(session_id: str) -> dict[str, Any]:
    """The conversation's audit events and its queued effects with their delivery status (no
    payloads: no addresses, no e-mail text). Kept after the conversation itself expires."""
    _check_session_id(session_id)
    store = runtime.store
    try:
        events = store.audit(session_id)
        effects = store.effects(session_id)
    except StoreUnavailable as exc:
        raise _store_unavailable() from exc
    if not events and not effects:
        raise ApiError("session_not_found", "Nothing is recorded for this conversation.", 404)
    return {"session_id": session_id, "events": events, "effects": effects}


@app.post("/api/sessions/{session_id}/messages")
async def post_message(
    session_id: str, body: NewMessage, request: Request, x_api_key: str | None = Header(default=None)
) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise ApiError("bad_request", "The message is empty.", 400)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # e.g. a lone surrogate from a \ud800 escape: not text
        raise ApiError("bad_request", "The message contains characters that are not valid text.", 400) from None
    message_id = body.message_id
    if message_id is not None and not MESSAGE_ID.fullmatch(message_id):
        raise ApiError("bad_request", "message_id must be 8 to 64 letters, digits, '-' or '_'.", 400)
    _check_session_id(session_id)
    _rate_limit(request, "turn")
    rt = runtime
    waited_from = time.perf_counter()
    if not await rt.slots.acquire(rt.settings.turn_slot_wait_seconds):
        rt.stats.count("refused_busy_503")
        _log_turn(session_id, "?", None, 503, "server_busy", time.perf_counter() - waited_from)
        raise ApiError(
            "server_busy",
            "The server is answering as many messages as it can right now; please try again shortly.",
            503,
            retry_after=rt.stats.retry_after(),
        )
    try:
        return await anyio.to_thread.run_sync(
            _run_turn, rt, session_id, text, x_api_key, message_id, limiter=_turn_thread_limiter(rt.slots.size)
        )
    finally:
        rt.slots.release()


def _answered(state: SessionState, message_id: str, text: str) -> tuple[str, dict[str, Any] | None] | None:
    """The stored answer to the caller message that carried `message_id`: (reply, trace), or
    None if the transcript holds no message with that id. The same id with other text is a
    client error (409 message_id_reused), not a retry."""
    messages = state.messages
    for index, message in enumerate(messages):
        if not (isinstance(message, dict) and message.get("role") == "user" and message.get("id") == message_id):
            continue
        if message.get("text") != text:
            raise ApiError(
                "message_id_reused",
                "This message id was already used for a different message; a new message needs a new id.",
                409,
            )
        turn = message.get("turn")
        reply = next(
            (
                str(later.get("text", ""))
                for later in messages[index + 1:]
                if isinstance(later, dict) and later.get("role") == "assistant" and later.get("turn") == turn
            ),
            "",
        )
        trace = next((t for t in state.traces if isinstance(t, dict) and t.get("turn") == turn), None)
        return reply, trace
    return None


def _run_turn(
    rt: Runtime, session_id: str, text: str, api_key: str | None, message_id: str | None = None
) -> dict[str, Any]:
    """One turn, on a worker thread, holding a turn slot.

    With a `message_id` that the transcript already holds (a retry of a message that was
    answered), the stored answer is returned instead ("replayed": true): the engine does not run
    and nothing is written. Otherwise the turn runs, and the effects it decided on (the summary
    e-mail, the handoff) are queued with its commit, for the dispatcher to deliver."""
    started = time.perf_counter()
    status, code, phase_before = 200, "", "?"  # "?": the conversation was never read
    trace: dict[str, Any] | None = None
    stored: tuple[str, dict[str, Any] | None] | None = None
    queued: list[dict[str, Any]] = []
    try:
        with rt.store.turn(session_id, wait=rt.settings.conversation_wait_seconds) as held:
            state = held.state  # a private copy: the store hands one out per turn
            phase_before = state.phase.value
            # Before anything else, including the message cap: a retry of the last message a
            # conversation may hold still gets its answer.
            if message_id is not None:
                stored = _answered(state, message_id, text)
            if stored is None:
                backend = get_backend(api_key)
                outbox_before, had_escalation = len(state.outbox), state.escalation is not None
                try:
                    with llm.turn_budget(rt.settings.turn_budget_seconds):
                        reply, trace = agent.handle_turn(state, text, backend, message_id=message_id)
                except ConversationClosed as exc:
                    rt.stats.count("closed_at_message_limit_409")
                    raise ApiError("conversation_closed", str(exc), 409) from exc
                except LLMError as exc:
                    rt.stats.count("model_errors")
                    raise ApiError(exc.code, exc.message, exc.status, exc.retry_after) from exc
                except Exception as exc:  # noqa: BLE001 - surface as a clean API error
                    rt.stats.count("model_errors")
                    log.exception("turn failed")
                    raise ApiError("llm_error", f"The turn could not be processed ({type(exc).__name__}).", 502) from exc
                queued = turn_effects(state, outbox_before, had_escalation)
                if rt.closing:
                    # Past the shutdown's grace period: the store is about to close, so this turn
                    # is not committed; the client's retry runs it again (on another instance).
                    raise ApiError(
                        "server_busy", "This server is shutting down; please send the message again.", 503,
                        retry_after=1,
                    )
                # Committed only now, with its effects: a failed turn leaves the conversation
                # exactly as it was and queues nothing.
                held.commit(effects=queued)
    except ApiError as exc:
        status, code = exc.status, exc.code
        raise
    except SessionNotFound:
        status, code = 404, "session_not_found"
        raise ApiError("session_not_found", "This conversation no longer exists on the server.", 404) from None
    except ConversationBusy:
        status, code = 409, "conversation_busy"
        rt.stats.count("conflicts_409")
        raise ApiError(
            "conversation_busy",
            "This conversation is still answering an earlier message; please try again in a moment.",
            409,
            retry_after=1,
        ) from None
    except StoreUnavailable:
        status, code = 503, "server_busy"
        raise _store_unavailable() from None
    finally:
        elapsed = time.perf_counter() - started
        _log_turn(session_id, phase_before, trace, status, "replayed" if stored is not None else code, elapsed)
    if stored is not None:
        rt.stats.count("replayed_messages")
        try:
            deliveries = _deliveries(rt.store, session_id, state)
        except StoreError:  # the answer matters more than its delivery status
            deliveries = {}
        reply, trace = stored
        return {"reply": reply, "state": with_delivery(state.to_view(settings), deliveries), "trace": trace,
                "replayed": True}
    rt.stats.turn_answered(elapsed)
    # What this turn queued is shown as queued without asking the store again; a later read of
    # the conversation shows how its delivery went.
    view = with_delivery(state.to_view(settings), {effect["key"]: QUEUED for effect in queued})
    return {"reply": reply, "state": view, "trace": trace, "replayed": False}


def _log_turn(
    session_id: str, phase_before: str, trace: dict[str, Any] | None, status: int, code: str, seconds: float
) -> None:
    """One line per turn: no message text, no identifiers (the session id is cut to 8 characters)."""
    phase_after = trace.get("phase_after", phase_before) if trace and status == 200 else phase_before
    parts = [f"turn {session_id[:8]}", f"phase={phase_before}->{phase_after}", f"status={status}"]
    if code:
        parts.append(code)
    parts.append(f"latency={seconds * 1000:.0f}ms")
    if trace:
        calls, tokens_in, tokens_out = 0, 0, 0
        for event in trace.get("events", []):
            meta = event.get("detail") if event.get("kind") == "llm" else None
            if event.get("kind") == "decision" and isinstance(event.get("detail"), dict):
                meta = event["detail"].get("llm")
            if not isinstance(meta, dict):
                continue
            calls += 1
            tokens_in += meta.get("input_tokens") or 0
            tokens_out += meta.get("output_tokens") or 0
        parts.append(f"model_calls={calls}")
        if tokens_in or tokens_out:
            parts.append(f"tokens_in={tokens_in} tokens_out={tokens_out}")
    turn_log.info(" ".join(parts))


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
