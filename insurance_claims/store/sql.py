"""What the SQLite and PostgreSQL stores share: the layout, every statement, leases with renewal,
the outbox, the audit trail and the sweeps. The subclasses supply connections, the SQL dialect,
the DDL and how errors map onto StoreUnavailable.

Layout (schema version 2; times are epoch seconds as floating point):

    conversations  one small row per conversation, rewritten by each commit: `state` is the JSON
                   of `SessionState.to_record()` without the append-only lists; `turn`, `phase`,
                   `status`, `verified`, `party_id` are copies for querying; `lease_owner` and
                   `lease_until` hold the conversation for one turn.
    messages       one row per transcript entry, written once: `data` is the entry's JSON
    traces         one row per turn trace, written once
    audit_events   one row per audit event, written once; kept after the conversation is gone,
                   until the audit retention passes (`data` is never encrypted: no personal data)
    outbox         effects queued by commits for a delivery process (`claim_effects`)
    store_meta     schema version, conversation count, last sweep

A commit appends only the entries added during the turn (their positions continue from what was
loaded) and updates the row, atomically with the effects: one transaction in PostgreSQL, one
statement in SQLite (see sqlite.py for why). Loading reassembles the state from the row and the
child rows in `seq` order, in one statement, so a reader never sees half a commit.

Leases: a turn takes the conversation with one atomic UPDATE (owner, expiry); commit and failure
release it. While the turn runs, one daemon thread per store object renews the holds of all its
turns about every lease/3, for at most `turn_max_seconds`. If the process dies the renewals stop
and the conversation is free again within one lease. A commit whose lease was lost writes nothing
and raises ConversationBusy.

The same thread sweeps about once a minute (one process at a time, claimed through store_meta):
expired conversations with their messages, traces and finished effects; effects of conversations
that are gone once they are finished (or, still undelivered, after the audit retention); audit
events of conversations that are gone, after the audit retention. Pending effects outlive their
conversation until they are delivered or dead.
"""

from __future__ import annotations

import json
import logging
import marshal
import math
import os
import random
import socket
import threading
import time
import uuid
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, NamedTuple, TypeVar

from ..state import SessionState
from .base import (
    EFFECT_STATUSES,
    SPLIT_FIELDS,
    ConversationBusy,
    Effect,
    EncryptedDataError,
    SessionNotFound,
    SessionStore,
    StoreError,
    Turn,
    check_claim_args,
    check_finish_args,
    dumps,
    split_appended,
    state_from_record,
)
from .crypto import Cipher

log = logging.getLogger("sop.store")

SCHEMA_VERSION = 2
T = TypeVar("T")

# SessionState list -> its table. Every append-only list the state declares must have one.
TABLES = {"messages": "messages", "traces": "traces", "events": "audit_events"}
if set(SPLIT_FIELDS) != set(TABLES):  # pragma: no cover - a change to state.py needs one here
    raise ImportError(f"the SQL stores keep {sorted(TABLES)} as rows, but the state declares {SPLIT_FIELDS}")

_EVENT_KEYS = frozenset({"seq", "turn", "at", "type", "data"})
_WRAPPED = "__event__"  # an audit event of another shape, stored whole
_NO_ROWS = {"messages": 0, "traces": 0, "events": 0}

# ---------------------------------------------------------------------------
# Statements, in a neutral dialect: named parameters (:name), {now} for the store's current time
# (the database clock in PostgreSQL), {lock} for "skip rows another transaction has locked".
# ---------------------------------------------------------------------------
_TAKE = (
    "UPDATE conversations SET lease_owner = :owner, lease_until = {now} + :lease, updated_at = {now} "
    "WHERE id = :id AND updated_at >= {now} - :ttl "
    "AND (lease_owner IS NULL OR lease_until < {now} OR lease_owner = :owner)"
)
_WATCH = (
    "SELECT CASE WHEN updated_at >= {now} - :ttl THEN 1 ELSE 0 END, "
    "CASE WHEN lease_owner IS NOT NULL AND lease_until >= {now} THEN 1 ELSE 0 END "
    "FROM conversations WHERE id = :id"
)
# (The typed NULLs of the first branch give PostgreSQL the column types of the union.)
_LOAD = (
    "SELECT 0, version, CAST(NULL AS INTEGER), CAST(NULL AS DOUBLE PRECISION), CAST(NULL AS TEXT), state "
    "FROM conversations WHERE id = :id AND @cond "
    "UNION ALL SELECT 1, seq, NULL, NULL, NULL, data FROM messages WHERE session_id = :id "
    "UNION ALL SELECT 2, seq, NULL, NULL, NULL, data FROM traces WHERE session_id = :id "
    "UNION ALL SELECT 3, seq, turn, at, type, data FROM audit_events WHERE session_id = :id "
    "ORDER BY 1, 2"
)
_READ = _LOAD.replace("@cond", "updated_at >= {now} - :ttl")
_LOAD_LEASED = _LOAD.replace("@cond", "lease_owner = :owner")
_COMMIT = (
    "UPDATE conversations SET state = :state, version = version + 1, updated_at = {now}, turn = :turn, "
    "phase = :phase, status = :status, verified = :verified, party_id = :party_id, "
    "lease_owner = NULL, lease_until = 0 "
    "WHERE id = :id AND lease_owner = :owner AND version = :version"
)
_RELEASE = "UPDATE conversations SET lease_owner = NULL, lease_until = 0 WHERE id = :id AND lease_owner = :owner"
_EXISTS = "SELECT 1 FROM conversations WHERE id = :id"
_CREATE = (
    "INSERT INTO conversations (id, state, version, created_at, updated_at, turn, phase, status, "
    "verified, party_id, lease_owner, lease_until) "
    "VALUES (:id, :state, 1, {now}, {now}, :turn, :phase, :status, :verified, :party_id, NULL, 0) "
    "ON CONFLICT (id) DO NOTHING"
)
_INSERT_MESSAGE = (
    "INSERT INTO messages (session_id, seq, turn, role, message_id, created_at, data) "
    "VALUES (:sid, :seq, :turn, :role, :message_id, {now}, :data)"
)
_INSERT_TRACE = "INSERT INTO traces (session_id, seq, turn, data) VALUES (:sid, :seq, :turn, :data)"
_INSERT_EVENT = (
    "INSERT INTO audit_events (session_id, seq, turn, at, type, created_at, data) "
    "VALUES (:sid, :seq, :turn, :at, :type, {now}, :data)"
)
_ENQUEUE = (
    "INSERT INTO outbox (session_id, key, kind, payload, status, attempts, next_attempt_at, created_at) "
    "VALUES (:sid, :key, :kind, :payload, 'pending', 0, {now}, {now}) "
    "ON CONFLICT (session_id, key) DO NOTHING"
)
_COUNT_ADD = "UPDATE store_meta SET value = value + :n WHERE key = :key"
_EVICT = (
    "DELETE FROM conversations WHERE id IN (SELECT id FROM conversations WHERE id <> :id "
    "AND (lease_owner IS NULL OR lease_until < {now}) ORDER BY updated_at LIMIT :n {lock}) RETURNING id"
)
_EXPIRE = (
    "DELETE FROM conversations WHERE id IN (SELECT id FROM conversations WHERE updated_at < {now} - :ttl "
    "AND (lease_owner IS NULL OR lease_until < {now}) ORDER BY updated_at LIMIT :n {lock}) RETURNING id"
)
_DELETE_MESSAGES = "DELETE FROM messages WHERE session_id = :id"
_DELETE_TRACES = "DELETE FROM traces WHERE session_id = :id"
_DELETE_FINISHED_EFFECTS = "DELETE FROM outbox WHERE session_id = :id AND status <> 'pending'"
_SWEEP_CLAIM = "UPDATE store_meta SET value = {now} WHERE key = 'last_sweep_at' AND value <= {now} - :interval"
_SWEEP_FINISHED_EFFECTS = (
    "DELETE FROM outbox WHERE id IN (SELECT o.id FROM outbox o WHERE o.status <> 'pending' "
    "AND o.created_at < {now} - :ttl "
    "AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = o.session_id) LIMIT :n)"
)
_SWEEP_UNDELIVERED_EFFECTS = (
    "DELETE FROM outbox WHERE id IN (SELECT o.id FROM outbox o WHERE o.status = 'pending' "
    "AND o.created_at < {now} - :retention "
    "AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = o.session_id) LIMIT :n)"
)
_SWEEP_AUDIT = (
    "DELETE FROM audit_events WHERE (session_id, seq) IN (SELECT a.session_id, a.seq FROM audit_events a "
    "WHERE a.created_at < {now} - :retention "
    "AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = a.session_id) LIMIT :n)"
)
_AUDIT = "SELECT seq, turn, at, type, data FROM audit_events WHERE session_id = :id ORDER BY seq"
_EFFECTS = (
    "SELECT id, kind, key, status, attempts, created_at, sent_at, last_error FROM outbox "
    "WHERE session_id = :id ORDER BY id"
)
# A claim moves next_attempt_at to the end of the claim, so "due" is one indexed condition and a
# lapsed claim is due again by itself. The token stays on the row after finishing, which makes
# finishing twice with the same token harmless.
_CLAIM = (
    "UPDATE outbox SET claimed_by = :token, claimed_until = {now} + :lease, "
    "next_attempt_at = {now} + :lease, attempts = attempts + 1 "
    "WHERE id IN (SELECT id FROM outbox WHERE status = 'pending' AND next_attempt_at <= {now} "
    "ORDER BY next_attempt_at, id LIMIT :n {lock}) "
    "RETURNING id, session_id, kind, key, payload, attempts"
)
_FINISH = {
    "sent": (
        "UPDATE outbox SET status = 'sent', sent_at = COALESCE(sent_at, {now}), claimed_until = NULL "
        "WHERE id = :id AND claimed_by = :token AND status IN ('pending', 'sent')"
    ),
    "pending": (
        "UPDATE outbox SET next_attempt_at = {now} + :retry_in, last_error = :error, claimed_until = NULL "
        "WHERE id = :id AND claimed_by = :token AND status = 'pending'"
    ),
    "dead": (
        "UPDATE outbox SET status = 'dead', last_error = :error, claimed_until = NULL "
        "WHERE id = :id AND claimed_by = :token AND status IN ('pending', 'dead')"
    ),
}
_HELD = "SELECT COUNT(*) FROM conversations WHERE updated_at >= {now} - :ttl"
_LEASED = "SELECT COUNT(*) FROM conversations WHERE lease_owner IS NOT NULL AND lease_until >= {now}"
_EFFECT_COUNTS = "SELECT status, COUNT(*) FROM outbox GROUP BY status"


class Result(NamedTuple):
    """A statement's rows (all fetched) and its row count."""

    rows: list[Any]
    rowcount: int

    def one(self) -> Any:
        return self.rows[0] if self.rows else None

    def scalar(self) -> Any:
        return self.rows[0][0] if self.rows else None


class Exec:
    """One connection, lent for one store operation (see SqlStore._session)."""

    # Set as a transaction's last statement has run: a connection lost after this point may
    # have committed, so the operation is not retried.
    committing = False

    def run(self, sql: str, params: Mapping[str, Any] | None = None, *, write: bool = False) -> Result:
        raise NotImplementedError

    def many(self, sql: str, rows: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def transaction(self) -> AbstractContextManager[None]:
        raise NotImplementedError


class _Loaded(NamedTuple):
    state: SessionState
    version: int
    counts: dict[str, int]  # rows per list, i.e. where the next entry's seq starts
    baseline: dict[str, list[Any]] | None  # the lists as loaded, a separate copy


class _Lost(Exception):
    """The commit found the conversation no longer held by this turn."""


class _Exists(Exception):
    """create() found the id taken."""


def _text(value: Any, limit: int = 200) -> str | None:
    """A copy for an indexed column: a short string (or an integer) without NUL characters."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text or len(text) > limit or "\x00" in text:
        return None
    return text


def _int(value: Any, default: int | None = None) -> int | None:
    return value if type(value) is int else default


def _is_wrapped(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and _WRAPPED in value


def _event_columns(event: Any, seq: int, now: float) -> tuple[int | None, float, str, str]:
    """(turn, at, type, data) for an audit event. An event of the shape SessionState.audit makes
    is split into columns, `data` holding its data; any other shape is stored whole (still
    lossless), with the best columns it allows."""
    if (
        isinstance(event, dict)
        and event.keys() == _EVENT_KEYS
        and type(event["seq"]) is int
        and event["seq"] == seq
        and type(event["turn"]) is int
        and type(event["at"]) is float
        and math.isfinite(event["at"])
        and (event["at"] != 0 or math.copysign(1.0, event["at"]) > 0)  # SQLite would keep -0.0 as 0.0
        and _text(event["type"], 100) == event["type"]
        and not _is_wrapped(event["data"])
    ):
        return event["turn"], event["at"], event["type"], dumps(event["data"])
    fields = event if isinstance(event, dict) else {}
    at = fields.get("at")
    usable = isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
    return _int(fields.get("turn")), float(at) if usable else now, _text(fields.get("type"), 100) or "unknown", dumps({_WRAPPED: event})


def _parse_each(texts: list[str]) -> list[Any]:
    """The JSON value of each text (one stored row each), parsed in one call: per-row calls
    were half the CPU time of loading a long conversation."""
    if not texts:
        return []
    values = json.loads("[" + ",".join(texts) + "]")
    if len(values) != len(texts):
        raise ValueError("a stored row does not hold exactly one JSON value")
    return values


def _event_from_row(seq: int, turn: int | None, at: float, kind: str, value: Any) -> Any:
    """An audit event from its row (`value`: the parsed `data` column)."""
    if _is_wrapped(value):
        return value[_WRAPPED]
    return {"seq": seq, "turn": turn, "at": at, "type": kind, "data": value}


class SqlStore(SessionStore):
    shared = True
    SWEEP_INTERVAL = 60.0  # seconds between sweeps (across all processes)
    SWEEP_BATCH = 500  # conversations or rows deleted per transaction
    SWEEP_MAX_SECONDS = 2.0  # one sweep stops after this; the rest waits for the next one
    NOW_SQL = ":now"  # {now} in the statements
    LOCK_SQL = ""  # {lock} in the statements
    COUNT_KEYS: tuple[str, ...] = ("conversation_count",)  # store_meta rows that sum to the count

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_sessions: int,
        wait_seconds: float,
        lease_seconds: float,
        turn_max_seconds: float,
        audit_retention_seconds: float,
        data_key: bytes | None,
        clock: Callable[[], float] | None,
        background: bool,
    ) -> None:
        super().__init__(ttl_seconds, max_sessions, wait_seconds)
        self.lease_seconds = float(lease_seconds)
        self.turn_max_seconds = float(turn_max_seconds)
        if not (math.isfinite(self.lease_seconds) and self.lease_seconds > 0):
            raise ValueError("lease_seconds must be a positive number of seconds")
        if not (math.isfinite(self.turn_max_seconds) and self.turn_max_seconds > 0):
            raise ValueError("turn_max_seconds must be a positive number of seconds")
        self.audit_retention_seconds = max(0.0, float(audit_retention_seconds))
        self._cipher = Cipher(data_key)
        self.encrypted = self._cipher.enabled
        # The store's clock for this process's own bookkeeping (and, in SQLite, for every time
        # stored or compared: the file is on one machine).
        self._clock = clock or time.time
        self._owner_prefix = f"{socket.gethostname()[:24]}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._compiled: dict[str, str] = {}
        self._count_sql = (
            "SELECT COALESCE(SUM(value), 0) FROM store_meta WHERE key IN ("
            + ", ".join(f"'{key}'" for key in self.COUNT_KEYS)
            + ")"
        )
        # Turns in progress in this store object: owner token -> (conversation, renew until).
        self._held: dict[str, tuple[str, float]] = {}
        self._held_lock = threading.Lock()
        self.background = bool(background)
        self._bg_lock = threading.Lock()
        self._bg_thread: threading.Thread | None = None
        self._bg_stop = threading.Event()
        self._quiet_until: dict[str, float] = {}

    # -- what a subclass supplies -------------------------------------------------------------
    @abstractmethod
    def _session(self, fn: Callable[[Exec], T], *, retry: bool = True) -> T:
        """Run `fn` with a connection, mapping database errors onto StoreError/StoreUnavailable.
        With `retry`, an operation whose connection was lost before it could have committed is
        tried once more on a fresh one (every operation passed with retry=True is idempotent)."""

    @abstractmethod
    def _time_params(self) -> dict[str, float]:
        """The parameters {now} needs."""

    def _dialect(self, sql: str) -> str:
        return sql

    def _count_key(self) -> str:
        """The store_meta row this transaction adds its conversations to."""
        return self.COUNT_KEYS[0]

    def _close_connections(self) -> None:
        return None

    def _compile(self, sql: str) -> str:
        compiled = self._compiled.get(sql)
        if compiled is None:
            compiled = self._dialect(sql.replace("{now}", self.NOW_SQL).replace("{lock}", self.LOCK_SQL))
            if len(self._compiled) < 512:
                self._compiled[sql] = compiled
        return compiled

    def _warn(self, key: str, message: str, *args: Any) -> None:
        """A warning at most once a minute per kind, so an outage does not flood the log."""
        now = time.monotonic()
        if now >= self._quiet_until.get(key, 0.0):
            self._quiet_until[key] = now + 60.0
            log.warning(message, *args)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.describe()}>"

    # -- conversations ----------------------------------------------------------------------
    def create(self, state: SessionState) -> None:
        self._start_background()
        session_id = state.session_id
        try:
            record = state.to_record()
            appended = split_appended(record, None)
            row = self._state_params(session_id, record)
            children = self._child_rows(session_id, appended, _NO_ROWS)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"conversation {session_id[:8]} could not be serialised: {exc}") from exc
        key = self._count_key()

        def write(ex: Exec) -> int:
            with ex.transaction():
                if ex.run(_CREATE, row, write=True).rowcount != 1:
                    raise _Exists()
                self._insert_children(ex, children)
                ex.run(_COUNT_ADD, {"key": key, "n": 1}, write=True)
                held = int(ex.run(self._count_sql).scalar() or 0)
                evicted: list[str] = []
                if held > self.max_sessions:
                    # Least recently used first; never one that a live turn holds.
                    evicted = self._delete_conversations(ex, _EVICT, {"id": session_id, "n": held - self.max_sessions})
                    if evicted:
                        ex.run(_COUNT_ADD, {"key": key, "n": -len(evicted)}, write=True)
                ex.committing = True
                return len(evicted)

        try:
            evicted = self._session(write)
        except _Exists:
            raise StoreError(f"conversation {session_id[:8]} already exists") from None
        self._count("evicted", evicted)

    def read(self, session_id: str) -> SessionState:
        rows = self._session(lambda ex: ex.run(_READ, {"id": session_id, "ttl": self.ttl}).rows)
        loaded = self._assemble(session_id, rows, with_baseline=False)
        if loaded is None:
            raise SessionNotFound(session_id)
        return loaded.state

    @contextmanager
    def turn(self, session_id: str, wait: float | None = None) -> Iterator[Turn]:
        self._start_background()
        wait = self.wait_seconds if wait is None else max(0.0, wait)
        deadline = time.monotonic() + wait
        owner = f"{self._owner_prefix}-{uuid.uuid4().hex[:12]}"
        rows = self._acquire(session_id, owner, deadline)
        self._hold(owner, session_id)
        committed = False
        try:
            loaded = self._assemble(session_id, rows, with_baseline=True)
            if loaded is None:
                raise ConversationBusy(session_id)

            def commit(state: SessionState, effects: list[Effect]) -> None:
                nonlocal committed
                self._commit(session_id, owner, loaded, state, effects)
                committed = True

            yield Turn(loaded.state, commit)
        finally:
            self._unhold(owner)
            if not committed:
                self._release(session_id, owner)

    def _acquire(self, session_id: str, owner: str, deadline: float) -> list[Any]:
        """Take the conversation's lease (waiting for another turn's until `deadline`) and load
        it, on one connection; returns the load query's rows."""
        params = {"id": session_id, "owner": owner, "lease": self.lease_seconds, "ttl": self.ttl}

        def take(ex: Exec) -> list[Any] | None:
            # The lease: one atomic UPDATE that succeeds only if nobody holds a live lease (or
            # this very turn does: a retried attempt after a lost connection).
            if ex.run(_TAKE, params, write=True).rowcount == 1:
                return ex.run(_LOAD_LEASED, params).rows
            return None

        delay = 0.005
        tries = 0
        while True:
            rows = self._session(take)
            if rows is not None:
                return rows
            tries += 1
            # Not taken: missing, expired, or another turn (perhaps in another process) holds it.
            # Watch it with reads, which never wait for writers, until the hold is gone.
            while True:
                row = self._session(lambda ex: ex.run(_WATCH, params).one())
                if row is None or (not row[0] and not row[1]):
                    raise SessionNotFound(session_id)
                remaining = deadline - time.monotonic()
                if not row[1]:
                    if remaining <= 0 and tries >= 2:
                        raise ConversationBusy(session_id)  # free each time we looked, taken each time we tried
                    break  # free now: try to take it
                if remaining <= 0:
                    raise ConversationBusy(session_id)
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.05)

    def _commit(
        self, session_id: str, owner: str, loaded: _Loaded, state: SessionState, effects: list[Effect]
    ) -> None:
        if state.session_id != session_id:
            raise StoreError("a turn cannot change the conversation id")
        try:
            record = state.to_record()
            appended = split_appended(record, loaded.baseline)
            row = self._state_params(session_id, record)
            children = self._child_rows(session_id, appended, loaded.counts)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"conversation {session_id[:8]} could not be serialised: {exc}") from exc
        row.update(owner=owner, version=loaded.version)
        queued = [
            {
                "sid": session_id,
                "key": effect.key,
                "kind": effect.kind,
                "payload": self._cipher.seal(effect.payload, "outbox", session_id, effect.key),
            }
            for effect in effects
        ]

        try:
            self._session(lambda ex: self._write_turn(ex, row, children, queued))
        except _Lost:
            # The lease lapsed and another turn took the conversation (or it was removed): this
            # turn's result is discarded rather than overwriting newer state.
            gone = self._session(lambda ex: ex.run(_EXISTS, {"id": session_id}).one()) is None
            raise (SessionNotFound if gone else ConversationBusy)(session_id) from None

    def _write_turn(
        self,
        ex: Exec,
        row: dict[str, Any],
        children: dict[str, list[dict[str, Any]]],
        queued: list[dict[str, Any]],
    ) -> None:
        """A turn's commit, all or nothing: the state row (only while this turn still holds the
        lease, at the version it loaded; otherwise _Lost), the new list rows and the effects."""
        with ex.transaction():
            if ex.run(_COMMIT, row, write=True).rowcount != 1:
                raise _Lost()
            self._insert_children(ex, children)
            if queued:
                ex.many(_ENQUEUE, queued)
            ex.committing = True

    def _release(self, session_id: str, owner: str) -> None:
        try:
            self._session(lambda ex: ex.run(_RELEASE, {"id": session_id, "owner": owner}, write=True))
        except StoreError as exc:  # the lease lapses by itself
            log.warning("could not release the hold on conversation %s: %s", session_id[:8], exc)

    # -- rows <-> state -------------------------------------------------------------------------
    def _state_params(self, session_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session_id,
            "state": self._cipher.seal(dumps(record), "conversations", session_id),
            "turn": _int(record.get("turn"), 0),
            "phase": _text(record.get("phase"), 64),
            "status": _text(record.get("status"), 64),
            "verified": bool(record.get("verified")),
            "party_id": _text(record.get("party_id"), 128),
        }

    def _child_rows(
        self, session_id: str, appended: dict[str, list[Any]], counts: Mapping[str, int]
    ) -> dict[str, list[dict[str, Any]]]:
        seal = self._cipher.seal
        messages = []
        for i, message in enumerate(appended.get("messages", ())):
            seq = counts["messages"] + i
            fields = message if isinstance(message, dict) else {}
            messages.append(
                {
                    "sid": session_id,
                    "seq": seq,
                    "turn": _int(fields.get("turn")),
                    "role": _text(fields.get("role"), 32),
                    "message_id": _text(fields.get("id"), 128),
                    "data": seal(dumps(message), "messages", session_id, seq),
                }
            )
        traces = []
        for i, trace in enumerate(appended.get("traces", ())):
            seq = counts["traces"] + i
            fields = trace if isinstance(trace, dict) else {}
            traces.append(
                {
                    "sid": session_id,
                    "seq": seq,
                    "turn": _int(fields.get("turn")),
                    "data": seal(dumps(trace), "traces", session_id, seq),
                }
            )
        events = []
        now = self._clock()
        for i, event in enumerate(appended.get("events", ())):
            seq = counts["events"] + i
            turn, at, kind, data = _event_columns(event, seq, now)
            events.append({"sid": session_id, "seq": seq, "turn": turn, "at": at, "type": kind, "data": data})
        return {"messages": messages, "traces": traces, "events": events}

    @staticmethod
    def _insert_children(ex: Exec, children: dict[str, list[dict[str, Any]]]) -> None:
        if children["messages"]:
            ex.many(_INSERT_MESSAGE, children["messages"])
        if children["traces"]:
            ex.many(_INSERT_TRACE, children["traces"])
        if children["events"]:
            ex.many(_INSERT_EVENT, children["events"])

    def _assemble(self, session_id: str, rows: list[Any], *, with_baseline: bool) -> _Loaded | None:
        """The state from the load query's rows (None if the conversation's row was not among
        them), with a separate copy of the append-only lists for checking the commit."""
        head = None
        texts: dict[str, list[tuple[int, str]]] = {"messages": [], "traces": []}
        events: list[tuple[Any, ...]] = []
        for part, seq, turn, at, kind, data in rows:
            if part == 0:
                head = (int(seq), data)
            elif part == 1:
                texts["messages"].append((seq, data))
            elif part == 2:
                texts["traces"].append((seq, data))
            else:
                events.append((seq, turn, at, kind, data))
        if head is None:
            return None
        version, state_text = head
        for name, numbers in (
            ("messages", [seq for seq, _ in texts["messages"]]),
            ("traces", [seq for seq, _ in texts["traces"]]),
            ("audit_events", [event[0] for event in events]),
        ):
            if numbers != list(range(len(numbers))):
                raise StoreError(
                    f"conversation {session_id[:8]}: its {name} rows are not numbered 0..n-1 (changed by hand?)"
                )
        try:
            record = json.loads(self._cipher.open(state_text, "conversations", session_id))
            lists = {
                name: _parse_each([self._cipher.open(data, name, session_id, seq) for seq, data in items])
                for name, items in texts.items()
            }
            lists["events"] = [
                _event_from_row(seq, turn, at, kind, value)
                for (seq, turn, at, kind, _), value in zip(events, _parse_each([event[4] for event in events]))
            ]
            # The baseline is a deep copy (made in C): a turn that changes an entry in place must
            # not change it there too.
            baseline = marshal.loads(marshal.dumps(lists)) if with_baseline else None
            record.update(lists)
        except EncryptedDataError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise StoreError(f"conversation {session_id[:8]} is stored in a form this version cannot read: {exc}") from exc
        counts = {"messages": len(texts["messages"]), "traces": len(texts["traces"]), "events": len(events)}
        return _Loaded(state_from_record(record, session_id), version, counts, baseline)

    def _delete_conversations(self, ex: Exec, select_sql: str, params: dict[str, Any]) -> list[str]:
        """Delete the conversations a DELETE ... RETURNING id statement picks, with their
        messages, traces and finished effects (pending effects and audit events stay)."""
        ids = [row[0] for row in ex.run(select_sql, params, write=True).rows]
        if ids:
            rows = [{"id": session_id} for session_id in ids]
            ex.many(_DELETE_MESSAGES, rows)
            ex.many(_DELETE_TRACES, rows)
            ex.many(_DELETE_FINISHED_EFFECTS, rows)
        return ids

    # -- audit trail and outbox -------------------------------------------------------------------
    def audit(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._session(lambda ex: ex.run(_AUDIT, {"id": session_id}).rows)
        try:
            values = _parse_each([row[4] for row in rows])
            return [_event_from_row(*row[:4], value) for row, value in zip(rows, values)]
        except ValueError as exc:
            raise StoreError(f"the audit trail of conversation {session_id[:8]} cannot be read: {exc}") from exc

    def effects(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._session(lambda ex: ex.run(_EFFECTS, {"id": session_id}).rows)
        return [
            {
                "id": int(effect_id),
                "kind": kind,
                "key": key,
                "status": status,
                "attempts": int(attempts),
                "created_at": created_at,
                "sent_at": sent_at,
                "last_error": last_error,
            }
            for effect_id, kind, key, status, attempts, created_at, sent_at, last_error in rows
        ]

    def claim_effects(self, limit: int = 10, lease_seconds: float = 30.0) -> list[dict[str, Any]]:
        limit, lease_seconds = check_claim_args(limit, lease_seconds)
        token = uuid.uuid4().hex
        params = {"token": token, "lease": lease_seconds, "n": limit}
        # Not retried: a claim that went through on a lost connection is redelivered after its
        # lease instead of being claimed twice now.
        rows = self._session(lambda ex: ex.run(_CLAIM, params, write=True).rows, retry=False)
        claimed = []
        for effect_id, session_id, kind, key, payload, attempts in sorted(rows):
            try:
                data = json.loads(self._cipher.open(payload, "outbox", session_id, key))
            except (StoreError, ValueError) as exc:
                # Undeliverable as stored (wrong data key, altered row): set aside, not retried.
                log.warning("effect %s of conversation %s cannot be read: %s", effect_id, session_id[:8], exc)
                self.finish_effect(int(effect_id), token, ok=False, error=f"payload cannot be read: {exc}")
                continue
            claimed.append(
                {
                    "id": int(effect_id),
                    "session_id": session_id,
                    "kind": kind,
                    "key": key,
                    "payload": data,
                    "attempts": int(attempts),
                    "token": token,
                }
            )
        return claimed

    def finish_effect(
        self,
        effect_id: int,
        token: str,
        *,
        ok: bool,
        error: str | None = None,
        retry_in: float | None = None,
    ) -> bool:
        status, error, retry_in = check_finish_args(ok, error, retry_in)
        params = {"id": int(effect_id), "token": str(token), "error": error, "retry_in": retry_in}
        return self._session(lambda ex: ex.run(_FINISH[status], params, write=True).rowcount) == 1

    # -- leases --------------------------------------------------------------------------------------
    def _hold(self, owner: str, session_id: str) -> None:
        with self._held_lock:
            self._held[owner] = (session_id, self._clock() + self.turn_max_seconds)

    def _unhold(self, owner: str) -> None:
        with self._held_lock:
            self._held.pop(owner, None)

    def renew_leases(self) -> int:
        now = self._clock()
        with self._held_lock:
            due = {owner: held for owner, held in self._held.items() if now <= held[1]}
            overdue = [held[0] for owner, held in self._held.items() if now > held[1]]
            for owner in [o for o, held in self._held.items() if now > held[1]]:
                del self._held[owner]
        for session_id in overdue:
            log.warning(
                "a turn on conversation %s has run for more than %g s: its hold is no longer renewed "
                "and lapses within %g s",
                session_id[:8],
                self.turn_max_seconds,
                self.lease_seconds,
            )
        if not due:
            return 0
        params: dict[str, Any] = {"lease": self.lease_seconds}
        ids = sorted({held[0] for held in due.values()})
        for i, session_id in enumerate(ids):
            params[f"i{i}"] = session_id
        for i, owner in enumerate(due):
            params[f"o{i}"] = owner
        sql = (
            "UPDATE conversations SET lease_until = {now} + :lease WHERE id IN ("
            + ", ".join(f":i{i}" for i in range(len(ids)))
            + ") AND lease_owner IN ("
            + ", ".join(f":o{i}" for i in range(len(due)))
            + ") RETURNING lease_owner"
        )
        renewed = {row[0] for row in self._session(lambda ex: ex.run(sql, params, write=True).rows)}
        lost = set(due) - renewed  # committed meanwhile, or lapsed and taken over
        if lost:
            with self._held_lock:
                for owner in lost:
                    self._held.pop(owner, None)
        return len(renewed)

    # -- background thread: renewals and sweeps ------------------------------------------------------
    def _start_background(self) -> None:
        if not self.background:
            return
        thread = self._bg_thread
        if thread is not None and thread.is_alive():
            return
        with self._bg_lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._background, args=(stop,), name=f"sop-store-{self.kind}-renewals", daemon=True
            )
            self._bg_stop, self._bg_thread = stop, thread
            thread.start()

    def _stop_background(self) -> None:
        with self._bg_lock:
            thread, self._bg_thread = self._bg_thread, None
            self._bg_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _background(self, stop: threading.Event) -> None:
        interval = max(0.05, self.lease_seconds / 3)
        next_sweep = time.monotonic() + random.uniform(1.0, 5.0)
        while not stop.wait(interval):
            try:
                if self._held:
                    self.renew_leases()
            except StoreError as exc:
                self._warn("renew", "renewing the holds of turns in progress failed: %s", exc)
            except Exception:  # noqa: BLE001 - the thread must outlive any one failure
                log.exception("renewing the holds of turns in progress failed")
            if time.monotonic() < next_sweep or stop.is_set():
                continue
            next_sweep = time.monotonic() + self.SWEEP_INTERVAL
            try:
                if self._session(lambda ex: ex.run(_SWEEP_CLAIM, {"interval": self.SWEEP_INTERVAL}, write=True).rowcount):
                    self.sweep()
            except StoreError as exc:
                self._warn("sweep", "sweeping expired conversations failed: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("sweeping expired conversations failed")

    def sweep(self) -> dict[str, int]:
        deadline = time.monotonic() + self.SWEEP_MAX_SECONDS
        key = self._count_key()

        def expire(ex: Exec) -> int:
            with ex.transaction():
                ids = self._delete_conversations(ex, _EXPIRE, {"ttl": self.ttl, "n": self.SWEEP_BATCH})
                if ids:
                    ex.run(_COUNT_ADD, {"key": key, "n": -len(ids)}, write=True)
                ex.committing = True
                return len(ids)

        def delete(sql: str) -> Callable[[Exec], int]:
            params = {"ttl": self.ttl, "retention": self.audit_retention_seconds, "n": self.SWEEP_BATCH}
            return lambda ex: ex.run(sql, params, write=True).rowcount

        done = {"conversations": self._repeat(expire, deadline)}
        self._count("expired", done["conversations"])
        done["effects"] = self._repeat(delete(_SWEEP_FINISHED_EFFECTS), deadline) + self._repeat(
            delete(_SWEEP_UNDELIVERED_EFFECTS), deadline
        )
        done["audit_events"] = self._repeat(delete(_SWEEP_AUDIT), deadline)
        return done

    def _repeat(self, fn: Callable[[Exec], int], deadline: float) -> int:
        total = 0
        while True:
            n = self._session(fn)
            total += n
            if n < self.SWEEP_BATCH or time.monotonic() >= deadline:
                return total

    # -- operations --------------------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        def query(ex: Exec) -> tuple[Any, Any, dict[str, int]]:
            held = ex.run(_HELD, {"ttl": self.ttl}).scalar()
            leased = ex.run(_LEASED).scalar()
            effects = {status: int(n) for status, n in ex.run(_EFFECT_COUNTS).rows}
            return held, leased, effects

        held, leased, effects = self._session(query)
        return {
            "kind": self.kind,
            "shared_between_processes": True,
            "sessions_held": int(held or 0),
            "turns_in_progress": int(leased or 0),
            "max_sessions": self.max_sessions,
            "ttl_seconds": self.ttl,
            "lease_seconds": self.lease_seconds,
            "turn_max_seconds": self.turn_max_seconds,
            "audit_retention_seconds": self.audit_retention_seconds,
            "encrypted_at_rest": self.encrypted,
            "effects": {status: effects.get(status, 0) for status in EFFECT_STATUSES},
            "scope": "sessions_held, turns_in_progress and effects: every process sharing the database; "
            "evicted/expired: this process",
            "evicted_since_start": self.evicted,
            "expired_since_start": self.expired,
            **self._extra_stats(),
        }

    def _extra_stats(self) -> dict[str, Any]:
        return {}

    def ping(self) -> None:
        self._session(lambda ex: ex.run("SELECT 1"))

    def close(self) -> None:
        self._stop_background()
        self._close_connections()
