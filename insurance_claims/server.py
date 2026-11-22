"""HTTP API + static test UI."""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import sop
from .agent import Agent
from .config import AGENT_NAME, APP_NAME, STATIC_DIR, load_settings, server_has_credentials
from .data import Repository
from .llm import AnthropicBackend, LLMError
from .mock_llm import MockBackend
from .state import SessionState

log = logging.getLogger("sop.server")

settings = load_settings()
repo = Repository.load()
agent = Agent(repo, settings)

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
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class SessionStore:
    """In-memory sessions (a demo store; swap for Redis or a database in production)."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[float, SessionState, threading.Lock]] = {}

    def put(self, state: SessionState) -> None:
        with self._lock:
            now = time.time()
            for sid in [s for s, (t, _, _) in self._sessions.items() if now - t > self._ttl]:
                del self._sessions[sid]
            existing = self._sessions.get(state.session_id)
            lock = existing[2] if existing else threading.Lock()
            self._sessions[state.session_id] = (now, state, lock)

    def get(self, session_id: str) -> tuple[SessionState, threading.Lock]:
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            raise ApiError("session_not_found", "This conversation no longer exists on the server.", 404)
        return entry[1], entry[2]


store = SessionStore(settings.session_ttl_seconds)
_backends: dict[str, Any] = {}
_backends_lock = threading.Lock()


def get_backend(api_key: str | None) -> Any:
    if settings.llm_mode == "mock":
        return _backends.setdefault("mock", MockBackend())
    key = (api_key or "").strip() or None
    cache_key = "server" if key is None else hashlib.sha256(key.encode()).hexdigest()
    with _backends_lock:
        backend = _backends.get(cache_key)
        if backend is None:
            try:
                backend = AnthropicBackend(settings, key)
            except LLMError as exc:
                raise ApiError(exc.code, exc.message, exc.status) from exc
            if len(_backends) > 64:
                _backends.clear()
            _backends[cache_key] = backend
        return backend


app = FastAPI(title=APP_NAME, docs_url="/api/docs", redoc_url=None)


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Unknown routes and methods use the same error envelope as everything else."""
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "bad_request")
    return JSONResponse(
        {"error": {"code": code, "message": str(exc.detail)}}, status_code=exc.status_code
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "bad_request", "message": "The request body is not valid."}}, status_code=400
    )


class NewSession(BaseModel):
    consent_scenario: str = "default"


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
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
        },
        "sample_prompts": SAMPLE_PROMPTS,
        "fixtures_exposed": settings.expose_fixtures,
    }


@app.get("/api/fixtures")
def get_fixtures() -> dict[str, Any]:
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
def create_session(body: NewSession | None = None) -> dict[str, Any]:
    state = agent.new_session((body.consent_scenario if body else "default"))
    store.put(state)
    return {
        "session_id": state.session_id,
        "messages": state.messages,
        "state": state.to_view(settings),
    }


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    state, _ = store.get(session_id)
    return {
        "session_id": state.session_id,
        "messages": state.messages,
        "state": state.to_view(settings),
        "traces": state.traces,
    }


@app.post("/api/sessions/{session_id}/messages")
def post_message(
    session_id: str, body: NewMessage, x_api_key: str | None = Header(default=None)
) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        raise ApiError("bad_request", "The message is empty.", 400)
    _, lock = store.get(session_id)
    backend = get_backend(x_api_key)
    with lock:  # one turn at a time per conversation
        # Read the state only once the lock is held, so a second request that was waiting
        # sees the first one's result instead of overwriting it.
        state, _ = store.get(session_id)
        # Work on a copy and commit only on success, so a failed model call (bad key,
        # rate limit, outage) leaves the conversation exactly as it was and can be retried.
        working = copy.deepcopy(state)
        try:
            reply, trace = agent.handle_turn(working, text, backend)
        except LLMError as exc:
            raise ApiError(exc.code, exc.message, exc.status) from exc
        except Exception as exc:  # noqa: BLE001 - surface as a clean API error
            log.exception("turn failed")
            raise ApiError("llm_error", f"The turn could not be processed ({type(exc).__name__}).", 502) from exc
        store.put(working)
    return {"reply": reply, "state": working.to_view(settings), "trace": trace}


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
