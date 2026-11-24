"""Session stores: where conversations live between turns.

The server uses one small interface for everything:

    store.create(state)                      a new conversation
    store.read(session_id) -> SessionState   a snapshot (SessionNotFound if missing or expired)
    with store.turn(session_id) as t:        exclusive access to one conversation, across threads
        ... mutate t.state ...               and processes; t.state is a private copy
        t.commit(effects=[...])              write it, and queue effects in the same transaction;
                                             nothing is written if the block raises
    store.audit(session_id)                  the audit trail, oldest first ([] if none is known)
    store.effects(session_id)                queued effects, oldest first, without payloads
    store.claim_effects(limit, lease_seconds) / store.finish_effect(id, token, ok=...)
                                             the outbox, for a delivery process
    store.stats(), store.ping(), store.describe(), store.close()

`turn()` waits a bounded time for a conversation that is mid-turn, then raises ConversationBusy
(the server answers 409 with Retry-After) instead of holding a worker thread indefinitely.

An effect is `{"kind": str, "key": str, "payload": dict}`: an external action (an e-mail, a
handoff) recorded atomically with the turn that decided it. A key the conversation has queued
before is ignored, so a retried turn cannot queue the same action twice. `claim_effects` hands
each due effect to one caller at a time for `lease_seconds`; `finish_effect` records it sent, due
again after `retry_in` seconds, or dead; a claim that lapses makes the effect due again (delivery
is at least once: a delivery process uses the key to recognise a repeat).

Three implementations:

- MemoryStore (default): one process, for development. See memory.py.
- SqliteStore (`SOP_SESSION_STORE=sqlite:///path/sessions.db` or a plain path): processes and
  instances on one machine sharing a file. See sqlite.py.
- PostgresStore (`SOP_SESSION_STORE=postgresql://user:password@host:5432/db`): instances on any
  number of machines sharing a database. See postgres.py.

The two SQL stores share one implementation (sql.py): a conversation is a small row plus its
messages, traces and audit events as rows of their own, written once each; a turn's commit
appends only what the turn added. Leases are renewed while a turn runs; the audit trail outlives
the conversation for SOP_AUDIT_RETENTION_DAYS; with SOP_DATA_KEY set, state, messages, traces and
effect payloads are encrypted at rest (AES-256-GCM; audit events stay readable). Schema version 2
uses new table names: a SQLite file written by the previous version keeps its `sessions` table,
which is neither read nor changed.

"Last use" is the creation of a conversation, or the start or the commit of one of its turns.
Reading a conversation does not count as use, so a GET never writes.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import parse_data_key
from .base import (
    ConversationBusy,
    EncryptedDataError,
    SessionNotFound,
    SessionStore,
    StoreError,
    StoreUnavailable,
    Turn,
    decode,
    encode,
)
from .memory import MemoryStore
from .postgres import PostgresStore, redact_url
from .sqlite import SqliteStore

__all__ = [
    "ConversationBusy",
    "EncryptedDataError",
    "MemoryStore",
    "PostgresStore",
    "SessionNotFound",
    "SessionStore",
    "SqliteStore",
    "StoreError",
    "StoreUnavailable",
    "Turn",
    "decode",
    "encode",
    "open_store",
    "parse_store_spec",
    "redact_url",
]

log = logging.getLogger("sop.store")

_SUPPORTED = (
    "use memory, sqlite:///path/sessions.db, a plain file path, or "
    "postgresql://user:password@host:5432/database"
)


def parse_store_spec(spec: str | None) -> tuple[str, str | None]:
    """("memory", None), ("sqlite", path) or ("postgres", url) for a SOP_SESSION_STORE value.

    memory | sqlite:///relative/path.db | sqlite:////absolute/path.db (or sqlite:///C:/path.db)
    | a plain file path | postgresql://... or postgres://... (a libpq URL).
    Error messages never contain a password.
    """
    value = (spec or "").strip()
    lower = value.lower()
    if not value or lower == "memory":
        return "memory", None
    if lower.startswith(("postgresql://", "postgres://")):
        return "postgres", value
    if lower.startswith("sqlite:///"):
        path = value[len("sqlite:///"):]
    elif "://" in value or lower.startswith("sqlite:"):
        raise ValueError(f"unsupported SOP_SESSION_STORE {redact_url(value)!r}: {_SUPPORTED}")
    else:
        path = value
    if not path or path == ":memory:":
        raise ValueError("SOP_SESSION_STORE needs a file path for SQLite (shared by every worker)")
    return "sqlite", path


def open_store(settings: Any) -> SessionStore:
    kind, where = parse_store_spec(settings.session_store)
    if kind == "memory":
        return MemoryStore(
            ttl_seconds=settings.session_ttl_seconds,
            max_sessions=settings.max_sessions,
            wait_seconds=settings.conversation_wait_seconds,
            max_bytes=settings.max_store_mb * 1024 * 1024,
        )
    key = parse_data_key(getattr(settings, "data_key", ""))
    common: dict[str, Any] = {
        "ttl_seconds": settings.session_ttl_seconds,
        "max_sessions": settings.max_sessions,
        "wait_seconds": settings.conversation_wait_seconds,
        "lease_seconds": settings.turn_lease_seconds,
        "turn_max_seconds": settings.turn_max_seconds,
        "audit_retention_seconds": settings.audit_retention_seconds,
        "data_key": key,
    }
    store: SessionStore
    if kind == "sqlite":
        store = SqliteStore(where, **common)  # type: ignore[arg-type]
    else:
        store = PostgresStore(where, pool_size=settings.store_pool_size, **common)  # type: ignore[arg-type]
    if key is None:
        log.warning(
            "%s keeps conversations unencrypted at rest; set SOP_DATA_KEY to encrypt them",
            store.describe(),
        )
    return store
