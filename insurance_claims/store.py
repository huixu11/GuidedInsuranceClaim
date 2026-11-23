"""Session stores: where conversations live between turns.

The server uses one small interface for everything:

    store.create(state)                      a new conversation
    store.read(session_id) -> SessionState   a snapshot (SessionNotFound if missing or expired)
    with store.turn(session_id) as t:        exclusive access to one conversation, across threads
        ... mutate t.state ...               and processes; t.state is a private copy
        t.commit()                           write it; nothing is written if the block raises
    store.stats(), store.close()

`turn()` waits a bounded time for a conversation that is mid-turn, then raises ConversationBusy
(the server answers 409 with Retry-After) instead of holding a worker thread indefinitely.

Two implementations, both keeping the state as the JSON text of `SessionState.to_record()`, so
every read and every turn works on a fresh copy and the serialisation is exercised on every
request whichever store is configured:

- MemoryStore (default): one process. Ordered by last use, so expiry and eviction are O(1);
  idle conversations expire after a TTL (checked on read as well as on write); at most
  `max_sessions` are held, and at most `max_bytes` of state, and beyond either the least
  recently used is evicted (it then answers 404, which the UI handles by starting afresh).
- SqliteStore (`SOP_SESSION_STORE=sqlite:///path/sessions.db` or a plain path): any number of
  worker processes or instances on one machine sharing the file serve any conversation, and
  conversations survive a restart. Exclusivity is a lease: one atomic UPDATE sets an owner and
  an expiry; commit and failure release it; it lapses by itself (after longer than a turn can
  take) so a crashed worker cannot lock a conversation forever; a commit whose lease was lost
  writes nothing. WAL mode (reads never wait for writes), one connection per thread, one
  writer per process at a time, a bounded wait for a lock another process holds, expired rows
  swept at most once a minute, the same cap.

"Last use" is the creation of a conversation, or the start or the commit of one of its turns.
Reading a conversation does not count as use, so a GET never writes.

Redis or Postgres would implement the same interface for instances on several machines: the
lease is `SET key owner NX PX <ms>` (released by a compare-and-delete script) in Redis, and
`SELECT ... FOR UPDATE` (or the same UPDATE with an owner column) in Postgres; the state is the
same JSON record. They are not included because they cannot be exercised in this environment,
and an untested store is worse than none.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .state import SessionState

log = logging.getLogger("sop.store")


class StoreError(Exception):
    """A store operation failed."""


class SessionNotFound(StoreError):
    """No such conversation: never created, expired, or evicted to make room."""


class ConversationBusy(StoreError):
    """Another turn holds the conversation (still, after the wait), or held it long enough for
    this turn's lease to lapse, in which case this turn's result was not written."""


class StoreUnavailable(StoreError):
    """The store could not be reached in time (for SQLite: locked beyond the busy timeout)."""


def encode(state: SessionState) -> str:
    return json.dumps(state.to_record(), separators=(",", ":"))


def decode(text: str) -> SessionState:
    return SessionState.from_record(json.loads(text))


class Turn:
    """One turn's access to one conversation: `state` is a private copy; `commit()` stores it."""

    __slots__ = ("state", "committed", "_commit")

    def __init__(self, state: SessionState, commit: Callable[[SessionState], None]) -> None:
        self.state = state
        self.committed = False
        self._commit = commit

    def commit(self) -> None:
        if self.committed:
            raise StoreError("this turn has already been committed")
        self._commit(self.state)
        self.committed = True


class SessionStore(ABC):
    kind: str = ""
    shared: bool = False  # True when other processes see the same conversations

    def __init__(self, ttl_seconds: float, max_sessions: int, wait_seconds: float) -> None:
        self.ttl = float(ttl_seconds)
        self.max_sessions = max(1, int(max_sessions))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._counter_lock = threading.Lock()
        self.evicted = 0
        self.expired = 0

    def _count(self, name: str, n: int = 1) -> None:
        if n:
            with self._counter_lock:
                setattr(self, name, getattr(self, name) + n)

    @abstractmethod
    def create(self, state: SessionState) -> None: ...

    @abstractmethod
    def read(self, session_id: str) -> SessionState: ...

    @abstractmethod
    def turn(self, session_id: str, wait: float | None = None) -> Any:
        """Context manager yielding a Turn; see the module docstring."""

    @abstractmethod
    def stats(self) -> dict[str, Any]: ...

    def close(self) -> None:
        """Release OS resources. The store stays usable (it reconnects when used again)."""


# ---------------------------------------------------------------------------
# In memory, one process
# ---------------------------------------------------------------------------
class _Entry:
    __slots__ = ("record", "used", "version", "busy", "waiters", "cond")

    def __init__(self, record: str, used: float) -> None:
        self.record = record
        self.used = used  # clock time of the last use
        self.version = 1
        self.busy = False  # a turn holds the conversation
        self.waiters = 0
        self.cond: threading.Condition | None = None


class MemoryStore(SessionStore):
    kind = "memory"
    shared = False

    def __init__(
        self,
        ttl_seconds: float = 4 * 3600,
        max_sessions: int = 50_000,
        wait_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = 0,
    ) -> None:
        super().__init__(ttl_seconds, max_sessions, wait_seconds)
        # The state held, as JSON text, is the process's main use of memory: a short conversation
        # is ~20 KB, one at the message cap up to ~400 KB. 0 = bounded by max_sessions alone.
        self.max_bytes = max(0, int(max_bytes))
        self._clock = clock
        self._lock = threading.Lock()
        # Least recently used first: every use moves the entry to the end.
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._busy = 0

    # -- internals (the caller holds self._lock) ------------------------------------
    def _drop(self, session_id: str, entry: _Entry) -> None:
        del self._entries[session_id]
        self._bytes -= len(entry.record)
        if entry.cond is not None:
            entry.cond.notify_all()  # waiters find it gone

    def _touch(self, session_id: str, entry: _Entry) -> None:
        entry.used = self._clock()
        self._entries.move_to_end(session_id)

    def _live(self, session_id: str) -> _Entry:
        entry = self._entries.get(session_id)
        if entry is None:
            raise SessionNotFound(session_id)
        if not entry.busy and self._clock() - entry.used > self.ttl:
            self._drop(session_id, entry)
            self._count("expired")
            raise SessionNotFound(session_id)
        return entry

    def _sweep(self) -> None:
        """Drop expired conversations from the old end; stops at the first live one."""
        now = self._clock()
        for _ in range(len(self._entries)):
            session_id, entry = next(iter(self._entries.items()))
            if entry.busy:  # mid-turn: in use, so it belongs at the recent end
                self._entries.move_to_end(session_id)
                continue
            if now - entry.used <= self.ttl:
                break
            self._drop(session_id, entry)
            self._count("expired")

    def _over(self) -> bool:
        return len(self._entries) > self.max_sessions or bool(self.max_bytes and self._bytes > self.max_bytes)

    def _evict(self) -> None:
        """Over the cap or the byte budget: drop the least recently used conversations that are
        not mid-turn."""
        while self._over():
            for _ in range(len(self._entries)):
                session_id, entry = next(iter(self._entries.items()))
                if entry.busy:
                    self._entries.move_to_end(session_id)
                    continue
                self._drop(session_id, entry)
                self._count("evicted")
                break
            else:
                return  # every conversation is mid-turn; bounded by the turn slots

    # -- interface ------------------------------------------------------------------
    def create(self, state: SessionState) -> None:
        record = encode(state)
        with self._lock:
            if state.session_id in self._entries:
                raise StoreError(f"conversation {state.session_id[:8]} already exists")
            self._sweep()
            self._entries[state.session_id] = _Entry(record, self._clock())
            self._bytes += len(record)
            self._evict()

    def read(self, session_id: str) -> SessionState:
        with self._lock:
            record = self._live(session_id).record
        return decode(record)

    @contextmanager
    def turn(self, session_id: str, wait: float | None = None) -> Iterator[Turn]:
        wait = self.wait_seconds if wait is None else max(0.0, wait)
        deadline = time.monotonic() + wait
        with self._lock:
            entry = self._live(session_id)
            while entry.busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConversationBusy(session_id)
                if entry.cond is None:
                    entry.cond = threading.Condition(self._lock)
                entry.waiters += 1
                try:
                    entry.cond.wait(remaining)
                finally:
                    entry.waiters -= 1
                if self._entries.get(session_id) is not entry:
                    raise SessionNotFound(session_id)  # expired or evicted meanwhile
            entry.busy = True
            self._busy += 1
            self._touch(session_id, entry)
            record, version = entry.record, entry.version
        try:
            state = decode(record)
            yield Turn(state, lambda s: self._commit(session_id, entry, version, s))
        finally:
            with self._lock:
                entry.busy = False
                self._busy -= 1
                if entry.waiters:
                    entry.cond.notify()  # type: ignore[union-attr]
                else:
                    entry.cond = None

    def _commit(self, session_id: str, entry: _Entry, version: int, state: SessionState) -> None:
        if state.session_id != session_id:
            raise StoreError("a turn cannot change the conversation id")
        record = encode(state)
        with self._lock:
            if self._entries.get(session_id) is not entry or entry.version != version:
                raise ConversationBusy(session_id)  # cannot happen while the entry is busy
            self._bytes += len(record) - len(entry.record)
            entry.record = record
            entry.version += 1
            self._touch(session_id, entry)
            self._sweep()
            self._evict()  # the state grew; this entry is mid-turn, so it is never the one dropped

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            held = len(self._entries)
            return {
                "kind": self.kind,
                "shared_between_processes": False,
                "sessions_held": held,
                "turns_in_progress": self._busy,
                "max_sessions": self.max_sessions,
                "ttl_seconds": self.ttl,
                "state_bytes_held": self._bytes,
                "max_state_bytes": self.max_bytes or None,
                "evicted_since_start": self.evicted,
                "expired_since_start": self.expired,
            }


# ---------------------------------------------------------------------------
# SQLite file, shared by processes on one machine
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,              -- JSON of SessionState.to_record()
    version     INTEGER NOT NULL,           -- +1 on every commit
    created_at  REAL NOT NULL,              -- unix time
    updated_at  REAL NOT NULL,              -- last use: TTL and eviction order
    lease_owner TEXT,                       -- the turn holding the conversation, or NULL
    lease_until REAL NOT NULL DEFAULT 0     -- the hold lapses by itself after this
);
CREATE INDEX IF NOT EXISTS sessions_by_use ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS sessions_leased ON sessions(lease_until) WHERE lease_owner IS NOT NULL;
CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
INSERT OR IGNORE INTO store_meta (key, value) VALUES ('sessions', 0), ('last_sweep', 0);
CREATE TRIGGER IF NOT EXISTS sessions_count_insert AFTER INSERT ON sessions
BEGIN UPDATE store_meta SET value = value + 1 WHERE key = 'sessions'; END;
CREATE TRIGGER IF NOT EXISTS sessions_count_delete AFTER DELETE ON sessions
BEGIN UPDATE store_meta SET value = value - 1 WHERE key = 'sessions'; END;
"""


class SqliteStore(SessionStore):
    kind = "sqlite"
    shared = True
    SWEEP_INTERVAL = 60.0  # seconds between sweeps of expired rows (across all processes)
    BUSY_TIMEOUT = 5.0  # seconds to keep trying while another process holds a lock we need
    RETRY_SLEEP = 0.001  # between those tries

    def __init__(
        self,
        path: str | os.PathLike[str],
        ttl_seconds: float = 4 * 3600,
        max_sessions: int = 50_000,
        wait_seconds: float = 5.0,
        lease_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(ttl_seconds, max_sessions, wait_seconds)
        self.path = os.fspath(path)
        if self.path in ("", ":memory:") or self.path.startswith("file:"):
            raise ValueError("the SQLite session store needs a file path that every process can open")
        self.lease_seconds = float(lease_seconds)
        self._clock = clock  # wall clock: shared by every process using the file
        self._owner_prefix = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._local = threading.local()
        self._conns: dict[threading.Thread, sqlite3.Connection] = {}
        self._conns_lock = threading.Lock()
        # SQLite admits one writer at a time. The threads of this process queue for it here, so
        # only one of them at a time contends with other processes for the file lock.
        self._write_lock = threading.Lock()
        self._next_sweep = 0.0  # monotonic; this process tries a sweep at most once a minute
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        with self._errors():
            conn = self._conn()
            self._write(conn, "PRAGMA journal_mode = WAL")
            with self._transaction(conn):
                for statement in _split_sql(_SCHEMA):
                    conn.execute(statement)
                # Re-count once at start, in case rows were changed by hand.
                conn.execute(
                    "UPDATE store_meta SET value = (SELECT COUNT(*) FROM sessions) WHERE key = 'sessions'"
                )

    # -- connections and statements -----------------------------------------------
    def _open(self) -> sqlite3.Connection:
        # No SQLite busy handler (timeout=0): _retry waits for locks instead (see there).
        conn = sqlite3.connect(self.path, timeout=0, isolation_level=None, check_same_thread=False)
        self._retry(conn, "PRAGMA synchronous = NORMAL")
        return conn

    def _conn(self) -> sqlite3.Connection:
        """This thread's connection (SQLite connections are not shared between threads)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
            with self._conns_lock:
                # Worker threads come and go (anyio retires idle ones): close what they left.
                for thread in [t for t in self._conns if not t.is_alive()]:
                    self._conns.pop(thread).close()
                self._conns[threading.current_thread()] = conn
        return conn

    def _retry(self, conn: sqlite3.Connection, sql: str, params: Any = ()) -> sqlite3.Cursor:
        """Run one statement; while another process holds a lock it needs, try again every
        millisecond for up to BUSY_TIMEOUT (then the OperationalError becomes StoreUnavailable).
        SQLite's own busy handler sleeps up to 100 ms between tries, so under load a waiter can
        lose the race to fresher writers again and again: seconds of tail latency, measured."""
        deadline = 0.0
        while True:
            try:
                return conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                now = time.monotonic()
                if not deadline:
                    deadline = now + self.BUSY_TIMEOUT
                elif now >= deadline:
                    raise
            time.sleep(self.RETRY_SLEEP)

    def _write(self, conn: sqlite3.Connection, sql: str, params: Any = ()) -> sqlite3.Cursor:
        """One write statement (its own transaction), taking this process's turn to write."""
        with self._write_lock:
            return self._retry(conn, sql, params)

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[None]:
        with self._write_lock:
            self._retry(conn, "BEGIN IMMEDIATE")  # the write lock now: no deadlocked upgrades
            try:
                yield
                self._retry(conn, "COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.OperationalError as exc:  # e.g. "database is locked" for BUSY_TIMEOUT
            raise StoreUnavailable(str(exc)) from exc

    # -- interface ------------------------------------------------------------------
    def create(self, state: SessionState) -> None:
        record = encode(state)
        now = self._clock()
        with self._errors():
            conn = self._conn()
            try:
                with self._transaction(conn):
                    conn.execute(
                        "INSERT INTO sessions (id, state, version, created_at, updated_at) "
                        "VALUES (?, ?, 1, ?, ?)",
                        (state.session_id, record, now, now),
                    )
                    count = conn.execute(
                        "SELECT value FROM store_meta WHERE key = 'sessions'"
                    ).fetchone()[0]
                    if count > self.max_sessions:
                        # Least recently used first; never one that a live turn holds.
                        evicted = conn.execute(
                            "DELETE FROM sessions WHERE id IN (SELECT id FROM sessions "
                            "WHERE id != ? AND (lease_owner IS NULL OR lease_until < ?) "
                            "ORDER BY updated_at LIMIT ?)",
                            (state.session_id, now, int(count - self.max_sessions)),
                        ).rowcount
                        self._count("evicted", evicted)
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"conversation {state.session_id[:8]} already exists") from exc
            self._maybe_sweep()

    def read(self, session_id: str) -> SessionState:
        with self._errors():
            row = self._retry(
                self._conn(),
                "SELECT state FROM sessions WHERE id = ? AND updated_at >= ?",
                (session_id, self._clock() - self.ttl),
            ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return decode(row[0])

    @contextmanager
    def turn(self, session_id: str, wait: float | None = None) -> Iterator[Turn]:
        wait = self.wait_seconds if wait is None else max(0.0, wait)
        deadline = time.monotonic() + wait
        owner = f"{self._owner_prefix}-{uuid.uuid4().hex[:12]}"
        delay = 0.005
        with self._errors():
            conn = self._conn()
            while True:
                now = self._clock()
                # The lease: one atomic UPDATE that succeeds only if nobody holds a live lease.
                taken = self._write(
                    conn,
                    "UPDATE sessions SET lease_owner = ?, lease_until = ?, updated_at = ? "
                    "WHERE id = ? AND updated_at >= ? AND (lease_owner IS NULL OR lease_until < ?)",
                    (owner, now + self.lease_seconds, now, session_id, now - self.ttl, now),
                ).rowcount
                if taken == 1:
                    break
                # Not taken: missing, expired, or another turn (perhaps in another process) holds
                # it. Watch it with reads, which never wait for writers, until the hold is gone.
                while True:
                    row = self._retry(
                        conn,
                        "SELECT updated_at, lease_owner, lease_until FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    now = self._clock()
                    leased = row is not None and row[1] is not None and row[2] >= now
                    if row is None or (row[0] < now - self.ttl and not leased):
                        raise SessionNotFound(session_id)
                    if not leased:
                        break  # free now: try to take it
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ConversationBusy(session_id)
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.05)
        committed = False
        try:
            with self._errors():
                row = self._retry(
                    conn,
                    "SELECT state, version FROM sessions WHERE id = ? AND lease_owner = ?",
                    (session_id, owner),
                ).fetchone()
            if row is None:
                raise ConversationBusy(session_id)
            version = row[1]

            def commit(state: SessionState) -> None:
                nonlocal committed
                self._commit(session_id, owner, version, state)
                committed = True

            yield Turn(decode(row[0]), commit)
        finally:
            if not committed:
                self._release(session_id, owner)

    def _commit(self, session_id: str, owner: str, version: int, state: SessionState) -> None:
        if state.session_id != session_id:
            raise StoreError("a turn cannot change the conversation id")
        record = encode(state)
        with self._errors():
            conn = self._conn()
            written = self._write(
                conn,
                "UPDATE sessions SET state = ?, version = version + 1, updated_at = ?, "
                "lease_owner = NULL, lease_until = 0 "
                "WHERE id = ? AND lease_owner = ? AND version = ?",
                (record, self._clock(), session_id, owner, version),
            ).rowcount
            if written != 1:
                # The lease lapsed and another turn took the conversation (or it was removed):
                # this turn's result is discarded rather than overwriting newer state.
                gone = self._retry(conn, "SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                raise SessionNotFound(session_id) if gone is None else ConversationBusy(session_id)
        self._maybe_sweep()

    def _release(self, session_id: str, owner: str) -> None:
        try:
            self._write(
                self._conn(),
                "UPDATE sessions SET lease_owner = NULL, lease_until = 0 WHERE id = ? AND lease_owner = ?",
                (session_id, owner),
            )
        except sqlite3.Error as exc:  # the lease lapses by itself
            log.warning("could not release the hold on conversation %s: %s", session_id[:8], exc)

    def _maybe_sweep(self) -> None:
        """Delete expired rows, at most once a minute across every process using the file."""
        if time.monotonic() < self._next_sweep:
            return
        self._next_sweep = time.monotonic() + self.SWEEP_INTERVAL
        now = self._clock()
        try:
            conn = self._conn()
            with self._transaction(conn):
                due = conn.execute(
                    "UPDATE store_meta SET value = ? WHERE key = 'last_sweep' AND value <= ?",
                    (now, now - self.SWEEP_INTERVAL),
                ).rowcount
                if due:
                    removed = conn.execute(
                        "DELETE FROM sessions WHERE updated_at < ? AND (lease_owner IS NULL OR lease_until < ?)",
                        (now - self.ttl, now),
                    ).rowcount
                    self._count("expired", removed)
        except sqlite3.Error as exc:  # housekeeping only; try again later
            log.warning("sweeping expired conversations failed: %s", exc)

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        with self._errors():
            conn = self._conn()
            held = self._retry(
                conn, "SELECT COUNT(*) FROM sessions WHERE updated_at >= ?", (now - self.ttl,)
            ).fetchone()[0]
            leased = self._retry(
                conn,
                "SELECT COUNT(*) FROM sessions WHERE lease_owner IS NOT NULL AND lease_until >= ?",
                (now,),
            ).fetchone()[0]
        return {
            "kind": self.kind,
            "shared_between_processes": True,
            "sessions_held": held,
            "turns_in_progress": leased,
            "max_sessions": self.max_sessions,
            "ttl_seconds": self.ttl,
            "lease_seconds": self.lease_seconds,
            "scope": "sessions_held and turns_in_progress: every process sharing the file; "
            "evicted/expired: this process",
            "evicted_since_start": self.evicted,
            "expired_since_start": self.expired,
        }

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._conns.values():
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
            self._local = threading.local()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """Another connection holds a lock this statement needs (SQLITE_BUSY / SQLITE_LOCKED)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "locked" in str(exc) or "busy" in str(exc)


def _split_sql(script: str) -> list[str]:
    """The schema's statements, one by one (trigger bodies contain semicolons)."""
    statements, current = [], []
    for line in script.strip().splitlines():
        line = line.split("--", 1)[0].rstrip()
        if not line:
            continue
        current.append(line)
        text = " ".join(current)
        if sqlite3.complete_statement(text):
            statements.append(text)
            current = []
    return statements


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def parse_store_spec(spec: str | None) -> tuple[str, str | None]:
    """("memory", None) or ("sqlite", path) for a SOP_SESSION_STORE value.

    memory | sqlite:///relative/path.db | sqlite:////absolute/path.db (or sqlite:///C:/path.db)
    | a plain file path.
    """
    value = (spec or "").strip()
    if not value or value.lower() == "memory":
        return "memory", None
    if value.lower().startswith("sqlite:///"):
        path = value[len("sqlite:///"):]
    elif "://" in value or value.lower().startswith("sqlite:"):
        raise ValueError(
            f"unsupported SOP_SESSION_STORE {value!r}: use memory, sqlite:///path/sessions.db "
            "or a plain file path"
        )
    else:
        path = value
    if not path or path == ":memory:":
        raise ValueError("SOP_SESSION_STORE needs a file path for SQLite (shared by every worker)")
    return "sqlite", path


def open_store(settings: Any) -> SessionStore:
    kind, path = parse_store_spec(settings.session_store)
    if kind == "memory":
        return MemoryStore(
            ttl_seconds=settings.session_ttl_seconds,
            max_sessions=settings.max_sessions,
            wait_seconds=settings.conversation_wait_seconds,
            max_bytes=settings.max_store_mb * 1024 * 1024,
        )
    return SqliteStore(
        path,  # type: ignore[arg-type]
        ttl_seconds=settings.session_ttl_seconds,
        max_sessions=settings.max_sessions,
        wait_seconds=settings.conversation_wait_seconds,
        lease_seconds=settings.turn_lease_seconds,
    )
