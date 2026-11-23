"""Run the demo server:  python -m insurance_claims [--mock] [--host H] [--port P] [--workers N]

Several worker processes (--workers N or SOP_WORKERS=N) need a session store they all share
(SOP_SESSION_STORE=sqlite:///path/sessions.db); see the README, "Scaling and limits".
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from typing import Any

import uvicorn

from .config import load_dotenv
from .store import parse_store_spec

_SESSION_IN_PATH = re.compile(r"(/api/sessions/[^/?#]{8})[^/?#]*")


class ShortSessionIds(logging.Filter):
    """Access-log lines show a conversation id cut to 8 characters, like the turn log: the full
    id is what lets anyone read the conversation back, so it does not belong in a log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], _SESSION_IN_PATH.sub(r"\1...", args[2]), *args[3:])
        return True


def logging_config(level: str) -> dict[str, Any]:
    """uvicorn's own log format, plus the application's loggers (sop.server, sop.turn, sop.store)
    on stderr. Passed to uvicorn so that every worker process configures it, not just this one."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"short_ids": {"()": "insurance_claims.__main__.ShortSessionIds"}},
        "formatters": {
            "default": {"()": "uvicorn.logging.DefaultFormatter", "fmt": "%(levelprefix)s %(message)s"},
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
            "app": {"format": "%(levelname)s:%(name)s:%(message)s"},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "filters": ["short_ids"],
                "stream": "ext://sys.stdout",
            },
            "app": {"class": "logging.StreamHandler", "formatter": "app", "stream": "ext://sys.stderr"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["app"], "level": level},
    }


def _env_workers() -> int:
    try:
        return int(os.environ.get("SOP_WORKERS", "").strip() or 1)
    except ValueError:
        return 1


def main(argv: list[str] | None = None) -> None:
    load_dotenv()  # the same .env the server reads, so SOP_WORKERS and SOP_SESSION_STORE count here too
    parser = argparse.ArgumentParser(description="SOP-guided insurance claims agent demo server")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument(
        "--mock",
        action="store_true",
        help="run offline with the regex/template test double instead of the model",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=_env_workers(),
        help="worker processes (default: SOP_WORKERS or 1); more than one needs "
        "SOP_SESSION_STORE=sqlite:///path/sessions.db",
    )
    args = parser.parse_args(argv)
    if args.mock:
        os.environ["SOP_LLM_MODE"] = "mock"  # read when the server module loads its settings
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    try:
        store_kind, _ = parse_store_spec(os.environ.get("SOP_SESSION_STORE"))
    except ValueError as exc:
        parser.error(str(exc))
    if args.workers > 1 and store_kind == "memory":
        parser.error(
            f"--workers {args.workers} needs a session store that every worker shares; with the "
            "in-memory store each worker would hold its own conversations and answer 404 for the "
            "others'. Set SOP_SESSION_STORE=sqlite:///path/sessions.db (e.g. /data/sessions.db "
            "in the container), or run one worker."
        )
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # workers is passed explicitly: left unset, uvicorn would read WEB_CONCURRENCY behind the
    # check above. proxy_headers is off so the client address is always the socket peer; the
    # one place that may trust X-Forwarded-For is the rate limiter, with SOP_TRUST_PROXY=1.
    uvicorn.run(
        "insurance_claims.server:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        proxy_headers=False,
        log_config=logging_config(level),
    )


if __name__ == "__main__":
    main()
