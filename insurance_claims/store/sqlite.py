"""SqliteStore: a file shared by the processes on one machine.

`SOP_SESSION_STORE=sqlite:///path/sessions.db` (or a plain path). Any number of worker processes
or instances on one machine sharing the file serve any conversation, and conversations survive a
restart. WAL mode (reads never wait for writes), one connection per thread, one writer per
process at a time (the threads of a process queue for it, so only one of them at a time contends
with other processes for the file lock), and a bounded wait, retried every millisecond, for a lock
another process holds. Times are this machine's wall clock (or the injected clock).

Schema version 2 (see sql.py for the layout). A file written by the previous version also holds
its `sessions` table: this version neither reads nor changes nor deletes it (its conversations
are not carried over; drop the table by hand once no process of the old version uses the file).
"""

from __future__ import annotations

import functools
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .base import StoreError, StoreUnavailable
from .sql import SCHEMA_VERSION, Exec, Result, SqlStore, T, _Lost

log = logging.getLogger("sop.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
INSERT OR IGNORE INTO store_meta (key, value) VALUES
    ('schema_version', 2), ('conversation_count', 0), ('last_sweep_at', 0);
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,              -- the record without its append-only lists (JSON or enc1:)
    version     INTEGER NOT NULL,           -- +1 on every commit
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,              -- last use: TTL and eviction order
    turn        INTEGER NOT NULL DEFAULT 0, -- copies of state fields, for querying
    phase       TEXT,
    status      TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    party_id    TEXT,
    lease_owner TEXT,                       -- the turn holding the conversation, or NULL
    lease_until REAL NOT NULL DEFAULT 0     -- the hold lapses by itself after this
);
CREATE INDEX IF NOT EXISTS conversations_by_use ON conversations (updated_at);
CREATE INDEX IF NOT EXISTS conversations_leased ON conversations (lease_until) WHERE lease_owner IS NOT NULL;
CREATE TABLE IF NOT EXISTS messages (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,           -- position in SessionState.messages
    turn        INTEGER,
    role        TEXT,
    message_id  TEXT,
    created_at  REAL NOT NULL,
    data        TEXT NOT NULL,              -- the entry (JSON or enc1:)
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS traces (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    turn        INTEGER,
    data        TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS audit_events (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    turn        INTEGER,
    at          REAL NOT NULL,              -- the event's own time
    type        TEXT NOT NULL,
    created_at  REAL NOT NULL,              -- when it was stored: the retention counts from here
    data        TEXT NOT NULL,              -- JSON, never encrypted
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS audit_events_by_age ON audit_events (created_at);
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    key             TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,          -- JSON or enc1:
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,          -- due from then (a claim moves it to the claim's end)
    claimed_by      TEXT,
    claimed_until   REAL,
    created_at      REAL NOT NULL,
    sent_at         REAL,
    last_error      TEXT,
    UNIQUE (session_id, key)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (next_attempt_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS outbox_by_age ON outbox (created_at);
"""

# A turn's commit is ONE statement, one call into SQLite. As a BEGIN ... COMMIT of several
# statements it would hold SQLite's write lock (one for every process on the file) while, between
# its statements, the thread waits to get the GIL back from the process's other turns: under load
# that stretched each commit to milliseconds and capped the throughput of every process on the file.
# So the commit is one multi-row INSERT into a TEMP view (TEMP objects belong to the thread's own
# connection, created as it opens): its INSTEAD OF triggers first stage the new rows (messages,
# traces, audit events, effects) in a TEMP table, then the last row, the conversation's, checks the
# lease, writes everything and empties the staging table. Atomic like any statement: RAISE (lease
# lost) or any error undoes all of it, the staging included.
_LEASE_LOST = "sop-lease-lost"
_COMMIT_COLUMNS = (
    "ord, part, id, owner, version, state, now, turn, phase, status, verified, party_id, "
    "seq, role, message_id, at, type, key, kind, data"
)
_STAGING = f"""
CREATE TEMP TABLE IF NOT EXISTS sop_staged (
    part       INTEGER NOT NULL,           -- 1 message, 2 trace, 3 audit event, 4 effect
    ord        INTEGER NOT NULL,           -- the order given
    seq        INTEGER,
    turn       INTEGER,
    role       TEXT,
    message_id TEXT,
    at         REAL,
    type       TEXT,
    key        TEXT,
    kind       TEXT,
    data       TEXT
);
CREATE TEMP VIEW IF NOT EXISTS sop_turn_commit AS SELECT
    NULL AS ord, NULL AS part, NULL AS id, NULL AS owner, NULL AS version, NULL AS state, NULL AS now,
    NULL AS turn, NULL AS phase, NULL AS status, NULL AS verified, NULL AS party_id, NULL AS seq,
    NULL AS role, NULL AS message_id, NULL AS at, NULL AS type, NULL AS key, NULL AS kind, NULL AS data;
CREATE TEMP TRIGGER IF NOT EXISTS sop_turn_commit_stage INSTEAD OF INSERT ON sop_turn_commit WHEN NEW.part > 0
BEGIN
    INSERT INTO sop_staged (part, ord, seq, turn, role, message_id, at, type, key, kind, data)
        VALUES (NEW.part, NEW.ord, NEW.seq, NEW.turn, NEW.role, NEW.message_id, NEW.at, NEW.type, NEW.key,
                NEW.kind, NEW.data);
END;
CREATE TEMP TRIGGER IF NOT EXISTS sop_turn_commit_write INSTEAD OF INSERT ON sop_turn_commit WHEN NEW.part = 0
BEGIN
    SELECT RAISE(ABORT, '{_LEASE_LOST}') WHERE NOT EXISTS (
        SELECT 1 FROM conversations WHERE id = NEW.id AND lease_owner = NEW.owner AND version = NEW.version);
    UPDATE conversations SET state = NEW.state, version = version + 1, updated_at = NEW.now,
        turn = NEW.turn, phase = NEW.phase, status = NEW.status, verified = NEW.verified,
        party_id = NEW.party_id, lease_owner = NULL, lease_until = 0
        WHERE id = NEW.id;
    INSERT INTO messages (session_id, seq, turn, role, message_id, created_at, data)
        SELECT NEW.id, seq, turn, role, message_id, NEW.now, data FROM sop_staged WHERE part = 1 ORDER BY ord;
    INSERT INTO traces (session_id, seq, turn, data)
        SELECT NEW.id, seq, turn, data FROM sop_staged WHERE part = 2 ORDER BY ord;
    INSERT INTO audit_events (session_id, seq, turn, at, type, created_at, data)
        SELECT NEW.id, seq, turn, at, type, NEW.now, data FROM sop_staged WHERE part = 3 ORDER BY ord;
    INSERT INTO outbox (session_id, key, kind, payload, status, attempts, next_attempt_at, created_at)
        SELECT NEW.id, key, kind, data, 'pending', 0, NEW.now, NEW.now FROM sop_staged WHERE part = 4 ORDER BY ord
        ON CONFLICT (session_id, key) DO NOTHING;
    DELETE FROM sop_staged;
END;
"""
_COMMIT_ROW = "(" + ", ".join("?" * 20) + ")"


@functools.lru_cache(maxsize=128)
def _turn_commit_sql(rows: int) -> str:
    """The commit statement for `rows` rows (the new ones, then the conversation's). Ordered
    explicitly: the triggers stage the new rows before the conversation's row writes them."""
    values = ", ".join([_COMMIT_ROW] * rows)
    return f"INSERT INTO temp.sop_turn_commit ({_COMMIT_COLUMNS}) SELECT * FROM (VALUES {values}) ORDER BY 1"


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """Another connection holds a lock this statement needs (SQLITE_BUSY / SQLITE_LOCKED)."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "locked" in str(exc) or "busy" in str(exc)


def _split_sql(script: str) -> list[str]:
    """The schema's statements, one by one."""
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


class _SqliteExec(Exec):
    def __init__(self, store: SqliteStore, conn: sqlite3.Connection) -> None:
        self.store = store
        self.conn = conn
        self.in_transaction = False
        self.committing = False

    def run(self, sql: str, params: Any = None, *, write: bool = False) -> Result:
        compiled = self.store._compile(sql)
        values = dict(params or ())
        if write and not self.in_transaction:
            # One write statement, its own transaction: take this process's turn to write.
            with self.store._write_lock:
                cursor = self.store._retry(self.conn, compiled, values)
                rows = cursor.fetchall()  # RETURNING rows; the statement completes here
                return Result(rows, cursor.rowcount)
        cursor = self.store._retry(self.conn, compiled, values)
        rows = cursor.fetchall()
        return Result(rows, cursor.rowcount)

    def many(self, sql: str, rows: list[dict[str, Any]]) -> None:
        if not self.in_transaction:
            raise RuntimeError("many() runs inside transaction()")
        now = self.store._clock()
        self.conn.executemany(self.store._compile(sql), [{**row, "now": now} for row in rows])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        store, conn = self.store, self.conn
        with store._write_lock:
            store._retry(conn, "BEGIN IMMEDIATE")  # the write lock now: no deadlocked upgrades
            self.in_transaction = True
            try:
                yield
                store._retry(conn, "COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                self.in_transaction = False


class SqliteStore(SqlStore):
    kind = "sqlite"
    BUSY_TIMEOUT = 5.0  # seconds to keep trying while another process holds a lock we need
    RETRY_SLEEP = 0.001  # between those tries
    MAX_ROWS_PER_STATEMENT = 1000  # a bigger commit (20 parameters a row) is a BEGIN ... COMMIT
    NOW_SQL = ":now"
    LOCK_SQL = ""

    def __init__(
        self,
        path: str | os.PathLike[str],
        ttl_seconds: float = 4 * 3600,
        max_sessions: int = 50_000,
        wait_seconds: float = 5.0,
        lease_seconds: float = 20.0,
        clock: Callable[[], float] | None = None,
        *,
        turn_max_seconds: float = 300.0,
        audit_retention_seconds: float = 90 * 86400.0,
        data_key: bytes | None = None,
        background: bool = True,
    ) -> None:
        self.path = os.fspath(path)
        if self.path in ("", ":memory:") or self.path.startswith("file:"):
            raise ValueError("the SQLite session store needs a file path that every process can open")
        super().__init__(
            ttl_seconds=ttl_seconds,
            max_sessions=max_sessions,
            wait_seconds=wait_seconds,
            lease_seconds=lease_seconds,
            turn_max_seconds=turn_max_seconds,
            audit_retention_seconds=audit_retention_seconds,
            data_key=data_key,
            clock=clock,
            background=background,
        )
        self._local = threading.local()
        self._conns: dict[threading.Thread, sqlite3.Connection] = {}
        self._conns_lock = threading.Lock()
        # SQLite admits one writer at a time. The threads of this process queue for it here, so
        # only one of them at a time contends with other processes for the file lock.
        self._write_lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        try:
            self._session(self._setup)
        except BaseException:
            self._close_connections()
            raise

    def _setup(self, ex: Exec) -> None:
        with self._write_lock:
            self._retry(ex.conn, "PRAGMA journal_mode = WAL")  # type: ignore[attr-defined]
        with ex.transaction():
            for statement in _split_sql(_SCHEMA):
                ex.run(statement)
            version = ex.run("SELECT value FROM store_meta WHERE key = 'schema_version'").scalar()
            if version is not None and version > SCHEMA_VERSION:
                raise StoreError(
                    f"{os.path.basename(self.path)} was written by a newer version of this program "
                    f"(store schema {int(version)}; this one knows {SCHEMA_VERSION})"
                )
            # Re-count once at start, in case rows were changed by hand.
            ex.run(
                "UPDATE store_meta SET value = (SELECT COUNT(*) FROM conversations) "
                "WHERE key = 'conversation_count'"
            )
            legacy = ex.run("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'").one()
        if legacy is not None:
            log.info(
                "%s also holds the 'sessions' table of the previous store version; it is left as it "
                "is (not read, not migrated)",
                os.path.basename(self.path),
            )

    # -- connections and statements -----------------------------------------------
    def _open(self) -> sqlite3.Connection:
        # No SQLite busy handler (timeout=0): _retry waits for locks instead (see there).
        conn = sqlite3.connect(self.path, timeout=0, isolation_level=None, check_same_thread=False)
        try:
            self._retry(conn, "PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")  # the staging table: no temporary files
            for statement in _split_sql(_STAGING):
                conn.execute(statement)
        except BaseException:
            conn.close()
            raise
        return conn

    def _write_turn(
        self,
        ex: Exec,
        row: dict[str, Any],
        children: dict[str, list[dict[str, Any]]],
        queued: list[dict[str, Any]],
    ) -> None:
        """The commit as one statement (see _STAGING), which holds the shared write lock only
        while SQLite itself works."""
        new_rows = sum(len(rows) for rows in children.values()) + len(queued)
        if new_rows >= self.MAX_ROWS_PER_STATEMENT:  # SQLite's limit on parameters (32766)
            return super()._write_turn(ex, row, children, queued)
        params: list[Any] = []
        n = 0
        for m in children["messages"]:
            params += (n, 1, None, None, None, None, None, m["turn"], None, None, None, None,
                       m["seq"], m["role"], m["message_id"], None, None, None, None, m["data"])
            n += 1
        for t in children["traces"]:
            params += (n, 2, None, None, None, None, None, t["turn"], None, None, None, None,
                       t["seq"], None, None, None, None, None, None, t["data"])
            n += 1
        for e in children["events"]:
            params += (n, 3, None, None, None, None, None, e["turn"], None, None, None, None,
                       e["seq"], None, None, e["at"], e["type"], None, None, e["data"])
            n += 1
        for q in queued:
            params += (n, 4, None, None, None, None, None, None, None, None, None, None,
                       None, None, None, None, None, q["key"], q["kind"], q["payload"])
            n += 1
        params += (n, 0, row["id"], row["owner"], row["version"], row["state"], self._clock(), row["turn"],
                   row["phase"], row["status"], row["verified"], row["party_id"],
                   None, None, None, None, None, None, None, None)
        conn: sqlite3.Connection = ex.conn  # type: ignore[attr-defined]
        try:
            with self._write_lock:
                self._retry(conn, _turn_commit_sql(n + 1), params)
        except sqlite3.IntegrityError as exc:
            if str(exc) == _LEASE_LOST:
                raise _Lost() from None
            raise

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
            if isinstance(params, dict) and ":now" in sql:
                params["now"] = self._clock()  # the time when the statement runs, not when it queued
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

    def _session(self, fn: Callable[[Exec], T], *, retry: bool = True) -> T:
        try:
            return fn(_SqliteExec(self, self._conn()))
        except sqlite3.OperationalError as exc:  # e.g. "database is locked" after BUSY_TIMEOUT
            raise StoreUnavailable(f"SQLite: {exc}") from exc
        except sqlite3.Error as exc:
            raise StoreError(f"SQLite: {exc}") from exc

    def _time_params(self) -> dict[str, float]:
        return {"now": self._clock()}

    def _close_connections(self) -> None:
        with self._conns_lock:
            for conn in self._conns.values():
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
            self._local = threading.local()

    def describe(self) -> str:
        return f"sqlite {self.path} (shared by every process on this machine that opens the file)"
