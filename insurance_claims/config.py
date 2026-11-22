"""Runtime settings for the SOP agent demo.

Everything is configured through environment variables (optionally loaded from a
`.env` file next to the repository root) so the same image runs locally, in
Docker and on a hosted demo without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FIXTURES_DIR = BASE_DIR / "fixtures"
STATIC_DIR = BASE_DIR / "static"

COMPANY_NAME = "Harbor Mutual"
AGENT_NAME = "Riley"
APP_NAME = f"{COMPANY_NAME} Claims Support — SOP Agent Demo"

# The fixture data was authored around early March 2026 (latest claim is dated
# 2026-03-01 and the appeal deadlines fall in March/April 2026). The agent
# reasons about dates, so by default it uses a clock that is consistent with the
# fixtures; set SOP_TODAY=real to use the wall clock instead.
DEFAULT_DEMO_TODAY = "2026-03-02"

DEFAULT_MODEL = "claude-opus-5-5"  # phrases the replies
# The structured calls (understand / resolve / summarize) are short extraction and
# classification requests on every turn. In live runs of the scripted scenarios the
# smaller model made the same decisions with noticeably shorter turns.
DEFAULT_UNDERSTAND_MODEL = "claude-haiku-5-5"

# Models that accept the server-side refusal fallback parameter.
REFUSAL_FALLBACK_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-fable-5-2",
        "claude-fable-5-1",
        "claude-mythos-5-2",
        "claude-mythos-5-1",
    }
)


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE per line). Real environment variables win."""
    path = path or (PROJECT_DIR / ".env")
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    llm_mode: str  # "anthropic" | "mock"
    model_respond: str
    model_understand: str
    effort_respond: str  # low | medium | high | xhigh | max | none (omit)
    effort_understand: str
    refusal_fallback: str  # auto | on | off
    demo_today: date
    expose_fixtures: bool

    # SOP limits (the numbers the harness enforces).
    required_id_matches: int = 3
    max_failed_attempts: int = 3
    out_of_scope_limit: int = 3
    max_persuasion_attempts: int = 2
    elevated_emotion_offer_streak: int = 3
    max_clarify_attempts: int = 3
    consent_max_polls: int = 3
    max_email_reasks: int = 3

    history_window: int = 40
    session_ttl_seconds: int = 4 * 3600

    def use_refusal_fallback(self, model: str) -> bool:
        if self.refusal_fallback == "off":
            return False
        if self.refusal_fallback == "on":
            return True
        return model in REFUSAL_FALLBACK_MODELS


def server_has_credentials() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    )


def load_settings() -> Settings:
    load_dotenv()
    model = _env("SOP_MODEL", DEFAULT_MODEL)
    today_raw = _env("SOP_TODAY", DEFAULT_DEMO_TODAY)
    if today_raw.lower() == "real":
        today = date.today()
    else:
        try:
            today = date.fromisoformat(today_raw)
        except ValueError:
            today = date.fromisoformat(DEFAULT_DEMO_TODAY)
    mode = _env("SOP_LLM_MODE", "anthropic").lower()
    if mode not in {"anthropic", "mock"}:
        mode = "anthropic"
    return Settings(
        llm_mode=mode,
        model_respond=model,
        model_understand=_env("SOP_MODEL_UNDERSTAND", DEFAULT_UNDERSTAND_MODEL),
        effort_respond=_env("SOP_EFFORT", "low").lower(),
        effort_understand=_env("SOP_EFFORT_UNDERSTAND", "low").lower(),
        refusal_fallback=_env("SOP_REFUSAL_FALLBACK", "auto").lower(),
        demo_today=today,
        expose_fixtures=_env_bool("SOP_EXPOSE_FIXTURES", True),
        # Configuration may tighten the SOP but not loosen it below its minimum: at least
        # three of the five identifiers, and limits that are at least 1.
        required_id_matches=min(5, max(3, _env_int("SOP_REQUIRED_ID_MATCHES", 3))),
        max_failed_attempts=max(1, _env_int("SOP_MAX_FAILED_ATTEMPTS", 3)),
        out_of_scope_limit=max(1, _env_int("SOP_OUT_OF_SCOPE_LIMIT", 3)),
        max_persuasion_attempts=max(1, _env_int("SOP_MAX_PERSUASION_ATTEMPTS", 2)),
    )
