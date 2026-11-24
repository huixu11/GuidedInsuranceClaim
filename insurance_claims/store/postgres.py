"""PostgresStore: one database shared by any number of instances on any number of machines.

`SOP_SESSION_STORE=postgresql://user:password@host:5432/database` (or `postgres://...`; libpq
query parameters such as `?sslmode=require` are passed through). Uses psycopg 3 with its
connection pool (`pip install "psycopg[binary,pool]"`).

- The same layout and statements as SQLite (sql.py); every time stored or compared is the
  database's clock (`statement_timestamp()`), so the instances' clocks do not matter.
- A connection is taken from the pool for one operation (taking a conversation and loading it,
  committing a turn, a read) and given back at once: none is held while a turn talks to the
  model. At most SOP_STORE_POOL_SIZE connections per process (per worker).
- Connecting times out after a few seconds, so do statements and lock waits on the server; no
  connection within the pool timeout, a lost connection or a timeout is StoreUnavailable (503).
  A connection found broken empties the pool of idle ones, and an operation that cannot have
  written yet (or that is safe to repeat) is tried once more on a fresh connection, so the same
  store object works again as soon as the database is back.
- The schema is created at start under an advisory lock, so instances starting together do not
  trip over each other. If the database cannot be reached at start, the store still starts (and
  answers StoreUnavailable) and creates the schema on first use once it can.
- Claims on the outbox and evictions use `FOR UPDATE SKIP LOCKED`: concurrent claimers never get
  the same effect and never wait for each other.
- The conversation count lives in 16 rows of store_meta (one picked per transaction) so that
  creations on many instances do not queue for one row.
- The password never leaves this module: not in logs, `describe()`, `repr()`, `stats()` or the
  errors raised here.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote

from .base import StoreError, StoreUnavailable
from .sql import SCHEMA_VERSION, Exec, Result, SqlStore, T

log = logging.getLogger("sop.store")

_STRIPES = 16
_COUNT_KEYS = tuple(f"conversation_count_{i}" for i in range(_STRIPES))
_SCHEMA_LOCK = 0x534F5053544F5245  # "SOPSTORE": one schema set-up at a time per database

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value DOUBLE PRECISION NOT NULL)",
    """CREATE TABLE IF NOT EXISTS conversations (
        id          TEXT PRIMARY KEY,
        state       TEXT NOT NULL,
        version     BIGINT NOT NULL,
        created_at  DOUBLE PRECISION NOT NULL,
        updated_at  DOUBLE PRECISION NOT NULL,
        turn        INTEGER NOT NULL DEFAULT 0,
        phase       TEXT,
        status      TEXT,
        verified    BOOLEAN NOT NULL DEFAULT FALSE,
        party_id    TEXT,
        lease_owner TEXT,
        lease_until DOUBLE PRECISION NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS conversations_by_use ON conversations (updated_at)",
    "CREATE INDEX IF NOT EXISTS conversations_leased ON conversations (lease_until) WHERE lease_owner IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS messages (
        session_id  TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        turn        INTEGER,
        role        TEXT,
        message_id  TEXT,
        created_at  DOUBLE PRECISION NOT NULL,
        data        TEXT NOT NULL,
        PRIMARY KEY (session_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS traces (
        session_id  TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        turn        INTEGER,
        data        TEXT NOT NULL,
        PRIMARY KEY (session_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        session_id  TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        turn        INTEGER,
        at          DOUBLE PRECISION NOT NULL,
        type        TEXT NOT NULL,
        created_at  DOUBLE PRECISION NOT NULL,
        data        TEXT NOT NULL,
        PRIMARY KEY (session_id, seq)
    )""",
    "CREATE INDEX IF NOT EXISTS audit_events_by_age ON audit_events (created_at)",
    """CREATE TABLE IF NOT EXISTS outbox (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session_id      TEXT NOT NULL,
        key             TEXT NOT NULL,
        kind            TEXT NOT NULL,
        payload         TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at DOUBLE PRECISION NOT NULL,
        claimed_by      TEXT,
        claimed_until   DOUBLE PRECISION,
        created_at      DOUBLE PRECISION NOT NULL,
        sent_at         DOUBLE PRECISION,
        last_error      TEXT,
        UNIQUE (session_id, key)
    )""",
    "CREATE INDEX IF NOT EXISTS outbox_due ON outbox (next_attempt_at, id) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS outbox_by_age ON outbox (created_at)",
    "INSERT INTO store_meta (key, value) VALUES ('schema_version', 2), ('last_sweep_at', 0), "
    + ", ".join(f"('{key}', 0)" for key in _COUNT_KEYS)
    + " ON CONFLICT (key) DO NOTHING",
)
_RECOUNT = (
    "UPDATE store_meta SET value = CASE WHEN key = :first THEN :n ELSE 0 END WHERE key IN ("
    + ", ".join(f"'{key}'" for key in _COUNT_KEYS)
    + ")"
)

# :name (not ::type casts) -> psycopg's %(name)s
_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
# scheme://user:password@ (libpq ends the user part at the first @ or /)
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:@/\s]*:)[^@/\s]*@")
_QUERY_PASSWORD = re.compile(r"(?i)([?&]password=)[^&\s]*")


def redact_url(text: str) -> str:
    """`text` with the password of any URL in it (user:password@, ?password=) replaced by ***."""
    return _QUERY_PASSWORD.sub(r"\1***", _URL_PASSWORD.sub(r"\1***@", str(text)))


def _secrets_of(url: str) -> set[str]:
    found = set()
    match = re.match(r"(?i)[a-z][a-z0-9+.-]*://[^:@/]*:([^@/]*)@", url)
    if match and match.group(1):
        found |= {match.group(1), unquote(match.group(1))}
    for value in re.findall(r"(?i)[?&]password=([^&]*)", url):
        if value:
            found |= {value, unquote(value)}
    return found


class _Unreachable(StoreUnavailable):
    """No connection could be made (as opposed to: every pooled connection was busy)."""


class _PgExec(Exec):
    def __init__(self, store: PostgresStore, conn: Any) -> None:
        self.store = store
        self.conn = conn
        self.committing = False

    def _values(self, compiled: str, params: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if "%(" not in compiled:
            return None
        values = self.store._time_params()
        if params:
            values.update(params)
        return values

    def run(self, sql: str, params: Mapping[str, Any] | None = None, *, write: bool = False) -> Result:
        compiled = self.store._compile(sql)
        cursor = self.conn.execute(compiled, self._values(compiled, params))
        rows = cursor.fetchall() if cursor.description is not None else []
        return Result(rows, cursor.rowcount)

    def many(self, sql: str, rows: list[dict[str, Any]]) -> None:
        compiled = self.store._compile(sql)
        extra = self.store._time_params()
        with self.conn.cursor() as cursor:
            cursor.executemany(compiled, [{**extra, **row} for row in rows])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn.transaction():
            yield


class PostgresStore(SqlStore):
    kind = "postgres"
    # The database's clock, the same for every instance (an injected clock, in tests, replaces it).
    NOW_SQL = "EXTRACT(EPOCH FROM statement_timestamp())::float8"
    LOCK_SQL = "FOR UPDATE SKIP LOCKED"
    COUNT_KEYS = _COUNT_KEYS
    CONNECT_TIMEOUT = 3  # seconds (libpq connect_timeout)
    STATEMENT_TIMEOUT_MS = 5000
    LOCK_TIMEOUT_MS = 3000
    POOL_TIMEOUT = 5.0  # seconds to wait for a connection from the pool
    RECONNECT_TIMEOUT = 5.0  # the pool gives up one connection attempt (with retries) after this
    OUTAGE_PAUSE = 1.0  # unreachable: requests fail at once for this long before one tries again

    def __init__(
        self,
        url: str,
        ttl_seconds: float = 4 * 3600,
        max_sessions: int = 50_000,
        wait_seconds: float = 5.0,
        lease_seconds: float = 20.0,
        clock: Callable[[], float] | None = None,
        *,
        turn_max_seconds: float = 300.0,
        audit_retention_seconds: float = 90 * 86400.0,
        data_key: bytes | None = None,
        pool_size: int = 20,
        background: bool = True,
        pool_timeout: float | None = None,
    ) -> None:
        self._secrets = _secrets_of(str(url))
        try:
            import psycopg
            import psycopg_pool  # noqa: F401
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            raise StoreError(
                'the PostgreSQL session store needs psycopg 3 and its pool: pip install "psycopg[binary,pool]"'
            ) from None
        self._psycopg = psycopg
        try:
            params = conninfo_to_dict(str(url))
        except psycopg.Error as exc:
            raise ValueError(f"SOP_SESSION_STORE is not a usable PostgreSQL URL: {self._safe(exc)}") from None
        if params.get("password"):
            self._secrets.add(str(params["password"]))
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
        self._test_clock = clock
        if clock is not None:
            # Tests: the injected clock is the time in every statement, as in SQLite, so that a
            # clock moved by hand gives exact results.
            self.NOW_SQL = ":now"
        self._where = "{user}{host}:{port}/{db}".format(
            user=f"{params['user']}@" if params.get("user") else "",
            host=params.get("host") or params.get("hostaddr") or "localhost",
            port=params.get("port") or 5432,
            db=params.get("dbname") or params.get("user") or "postgres",
        )
        self.pool_size = max(1, int(pool_size))
        self.pool_timeout = float(pool_timeout) if pool_timeout is not None else self.POOL_TIMEOUT
        options = " ".join(
            part
            for part in (
                str(params.get("options") or ""),
                f"-c statement_timeout={self.STATEMENT_TIMEOUT_MS}",
                f"-c lock_timeout={self.LOCK_TIMEOUT_MS}",
                "-c idle_in_transaction_session_timeout=60000",
            )
            if part
        )
        self._conninfo = str(url)
        self._connect_kwargs: dict[str, Any] = {
            "autocommit": True,
            "connect_timeout": self.CONNECT_TIMEOUT,
            "options": options,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
        if not params.get("application_name"):
            self._connect_kwargs["application_name"] = "sop-claims-agent"
        self._pool: Any = None
        self._pool_lock = threading.Lock()
        self._setup_lock = threading.Lock()
        self._ready = False
        self._purged_at = 0.0
        # While the database cannot be reached: fail at once, and let one request at a time try
        # again (at most once per OUTAGE_PAUSE), instead of every request waiting the pool timeout.
        self._down = False
        self._down_until = 0.0
        self._probe = threading.Lock()
        self._connect_errors_seen = 0
        try:
            self._ensure_ready()
        except StoreUnavailable as exc:
            # Start anyway: requests answer 503 until the database can be reached, then the
            # schema is set up on first use, without a restart.
            log.warning("PostgreSQL (%s) cannot be reached yet: %s", self._where, exc)
        except BaseException:
            self._close_connections()
            raise

    # -- connections ---------------------------------------------------------------------
    def _time_params(self) -> dict[str, float]:
        clock = self._test_clock
        return {} if clock is None else {"now": clock()}

    def _dialect(self, sql: str) -> str:
        if not _PARAM.search(sql):
            return sql
        return _PARAM.sub(r"%(\1)s", sql.replace("%", "%%"))

    def _count_key(self) -> str:
        return random.choice(self.COUNT_KEYS)

    def _get_pool(self) -> Any:
        pool = self._pool
        if pool is not None:
            return pool
        with self._pool_lock:
            if self._pool is None:
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(
                    self._conninfo,
                    min_size=1,
                    max_size=self.pool_size,
                    kwargs=self._connect_kwargs,
                    timeout=self.pool_timeout,
                    reconnect_timeout=self.RECONNECT_TIMEOUT,
                    name="sop-postgres-pool",
                    open=False,
                )
                pool.open(wait=False)
                self._pool = pool
            return self._pool

    def _purge(self) -> None:
        """A connection was found broken: the idle ones probably are too (the server restarted,
        the network dropped). Replace them, at most once a second."""
        now = time.monotonic()
        if now - self._purged_at < 1.0:
            return
        self._purged_at = now
        pool = self._pool
        if pool is not None:
            try:
                pool.drain()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def _safe(self, error: Any) -> str:
        text = str(error).strip() or type(error).__name__
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "***")
        return redact_url(text)

    def _run(self, fn: Callable[[Exec], T], retry: bool) -> T:
        probing = False
        if self._down:
            if time.monotonic() < self._down_until or not self._probe.acquire(blocking=False):
                raise StoreUnavailable(
                    "PostgreSQL: the database could not be reached a moment ago (tried again at most "
                    f"once every {self.OUTAGE_PAUSE:g} s)"
                )
            probing = True
        try:
            result = self._attempt(fn, retry)
        except _Unreachable:
            self._down_until = time.monotonic() + self.OUTAGE_PAUSE
            if not self._down:
                self._down = True
                log.warning("PostgreSQL (%s) cannot be reached; requests fail until it can", self._where)
            raise
        except BaseException:
            if probing:
                self._down = False  # it answered, even if not as hoped
            raise
        else:
            if self._down:
                self._down = False
                log.warning("PostgreSQL (%s) can be reached again", self._where)
            return result
        finally:
            if probing:
                self._probe.release()

    def _attempt(self, fn: Callable[[Exec], T], retry: bool) -> T:
        psycopg = self._psycopg
        from psycopg_pool import PoolTimeout

        attempt = 0
        while True:
            attempt += 1
            ex: _PgExec | None = None
            try:
                with self._get_pool().connection(timeout=self.pool_timeout) as conn:
                    ex = _PgExec(self, conn)
                    return fn(ex)
            except PoolTimeout:
                errors = int(self._get_pool().get_stats().get("connections_errors", 0))
                failing, self._connect_errors_seen = errors > self._connect_errors_seen, errors
                if failing:  # new connections failed meanwhile: the database cannot be reached
                    raise _Unreachable(
                        f"PostgreSQL: no connection within {self.pool_timeout:g} s: the database cannot be reached"
                    ) from None
                raise StoreUnavailable(
                    f"PostgreSQL: no connection within {self.pool_timeout:g} s: all {self.pool_size} "
                    "pooled connections are busy"
                ) from None
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                lost = ex is None or ex.conn.broken or ex.conn.closed
                if lost:
                    self._purge()
                # A deadlock or serialisation failure rolled the transaction back: safe to repeat.
                again = isinstance(exc, psycopg.errors.TransactionRollback) or lost
                if retry and again and attempt == 1 and not (ex is not None and ex.committing):
                    continue
                raise StoreUnavailable(f"PostgreSQL: {self._safe(exc)}") from None
            except psycopg.Error as exc:
                raise StoreError(f"PostgreSQL: {self._safe(exc)}") from None

    def _session(self, fn: Callable[[Exec], T], *, retry: bool = True) -> T:
        if not self._ready:
            self._ensure_ready()
        return self._run(fn, retry)

    # -- schema ----------------------------------------------------------------------------
    def _ensure_ready(self) -> None:
        with self._setup_lock:
            if self._ready:
                return
            self._run(self._setup, retry=True)
            self._ready = True
        try:
            self._run(self._recount, retry=False)
        except StoreError as exc:  # the count is kept exact by every write anyway
            log.info("re-counting the conversations at start was skipped: %s", exc)

    def _setup(self, ex: Exec) -> None:
        with ex.transaction():
            ex.run("SELECT pg_advisory_xact_lock(:key)", {"key": _SCHEMA_LOCK})
            for statement in _SCHEMA:
                ex.run(statement)
            version = ex.run("SELECT value FROM store_meta WHERE key = 'schema_version'").scalar()
            if version is not None and version > SCHEMA_VERSION:
                raise StoreError(
                    f"the database {self._where} was written by a newer version of this program "
                    f"(store schema {int(version)}; this one knows {SCHEMA_VERSION})"
                )
            ex.committing = True

    def _recount(self, ex: Exec) -> None:
        """Re-count once at start, in case rows were changed by hand. The SHARE lock waits for
        writers in progress and holds new ones off for the moment the count takes."""
        with ex.transaction():
            ex.run("SET LOCAL lock_timeout = '2s'")
            ex.run("LOCK TABLE conversations IN SHARE MODE")
            n = ex.run("SELECT COUNT(*) FROM conversations").scalar()
            ex.run(_RECOUNT, {"first": self.COUNT_KEYS[0], "n": int(n or 0)})
            ex.committing = True

    # -- operations --------------------------------------------------------------------------
    def describe(self) -> str:
        return f"postgres {self._where} (shared by every process using the database)"

    def _extra_stats(self) -> dict[str, Any]:
        pool = self._pool
        measures = pool.get_stats() if pool is not None else {}
        return {
            "pool": {
                "max_size": self.pool_size,
                "connections": measures.get("pool_size", 0),
                "idle": measures.get("pool_available", 0),
                "requests_waiting": measures.get("requests_waiting", 0),
            }
        }

    def _close_connections(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.close(timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
