"""The session store interface, its errors, and what every implementation shares."""

from __future__ import annotations

import json
import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from ..state import APPEND_ONLY_FIELDS, SessionState


class StoreError(Exception):
    """A store operation failed."""


class SessionNotFound(StoreError):
    """No such conversation: never created, expired, or evicted to make room."""


class ConversationBusy(StoreError):
    """Another turn holds the conversation (still, after the wait), or held it long enough for
    this turn's lease to lapse, in which case this turn's result was not written."""


class StoreUnavailable(StoreError):
    """The store could not be reached in time: SQLite locked beyond the busy timeout; for
    PostgreSQL no connection within the pool timeout, a lost connection or a statement timeout.
    The server answers 503 with Retry-After; the same store object works again once the database
    does."""


class EncryptedDataError(StoreError):
    """Stored data is encrypted and the configured data key (SOP_DATA_KEY) cannot open it: there
    is no key, or it is a different one, or the stored value was altered."""


# The append-only lists of SessionState that a store keeps as rows of their own (written once,
# never rewritten). Any other APPEND_ONLY_FIELDS entry stays inside the state record.
SPLIT_FIELDS = tuple(name for name in ("messages", "traces", "events") if name in APPEND_ONLY_FIELDS)

EFFECT_STATUSES = ("pending", "sent", "dead")
MAX_EFFECT_KIND_CHARS = 64
MAX_EFFECT_KEY_CHARS = 200
MAX_ERROR_CHARS = 500
MAX_CLAIM = 1000


def dumps(value: Any) -> str:
    """The one JSON form every store writes (ASCII only, so any text survives any database)."""
    return json.dumps(value, separators=(",", ":"))


def encode(state: SessionState) -> str:
    return dumps(state.to_record())


def decode(text: str) -> SessionState:
    return SessionState.from_record(json.loads(text))


def state_from_record(record: dict[str, Any], session_id: str) -> SessionState:
    """SessionState.from_record, with a store error (not a ValueError) for a record this version
    of the code cannot read, e.g. one written in a newer record format."""
    try:
        return SessionState.from_record(record)
    except (ValueError, TypeError, KeyError) as exc:
        raise StoreError(f"conversation {session_id[:8]} cannot be read by this version: {exc}") from exc


class Effect(NamedTuple):
    """An external action queued with a commit (an e-mail, a handoff), for a delivery process."""

    kind: str
    key: str  # unique within the conversation: queuing the same key again is ignored
    payload: str  # JSON text


def normalize_effects(effects: Iterable[Mapping[str, Any]] | None) -> list[Effect]:
    """Checks `[{"kind": str, "key": str, "payload": dict}, ...]` and keeps the first effect of
    each key. A malformed effect is a programming error (ValueError), raised before anything is
    written."""
    out: list[Effect] = []
    seen: set[str] = set()
    for effect in effects or ():
        if not isinstance(effect, Mapping) or set(effect) != {"kind", "key", "payload"}:
            raise ValueError('an effect is {"kind": str, "key": str, "payload": dict}')
        kind, key, payload = effect["kind"], effect["key"], effect["payload"]
        for name, value, limit in (("kind", kind, MAX_EFFECT_KIND_CHARS), ("key", key, MAX_EFFECT_KEY_CHARS)):
            if not isinstance(value, str) or not 0 < len(value) <= limit or "\x00" in value:
                raise ValueError(f"an effect's {name} must be a string of 1 to {limit} characters")
        if not isinstance(payload, Mapping):
            raise ValueError("an effect's payload must be a dict")
        try:
            text = dumps(dict(payload))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"an effect's payload must be JSON data: {exc}") from exc
        if key in seen:
            continue
        seen.add(key)
        out.append(Effect(kind, key, text))
    return out


def split_appended(record: dict[str, Any], baseline: Mapping[str, list[Any]] | None) -> dict[str, list[Any]]:
    """Takes the SPLIT_FIELDS lists out of `record` (a fresh `to_record()`) and returns the
    entries appended to each since `baseline`, the lists as the turn loaded them (None for a new
    conversation: every entry is new).

    The lists only ever grow: one that got shorter, or an entry loaded earlier that was changed
    in place, is a StoreError, raised before anything is written. (A store that keeps the lists
    as rows would otherwise drop such a change without a word.)
    """
    appended: dict[str, list[Any]] = {}
    for name in SPLIT_FIELDS:
        items = record.pop(name, None) or []
        if baseline is None:
            appended[name] = list(items)
            continue
        before = baseline.get(name) or []
        if len(items) < len(before):
            raise StoreError(
                f"{name} got shorter during a turn ({len(before)} -> {len(items)} entries); "
                "entries of an append-only list are never removed"
            )
        # The JSON comparison is a second opinion for values that are not equal to themselves
        # (NaN), which are unchanged if they serialise the same.
        if items[: len(before)] != before and dumps(items[: len(before)]) != dumps(before):
            changed = next(i for i, (a, b) in enumerate(zip(items, before)) if dumps(a) != dumps(b))
            raise StoreError(
                f"{name}[{changed}] was changed during a turn; entries of an append-only list are "
                "written once and never changed"
            )
        appended[name] = items[len(before):]
    return appended


def check_claim_args(limit: int, lease_seconds: float) -> tuple[int, float]:
    limit = int(limit)
    lease_seconds = float(lease_seconds)
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive number of seconds")
    return min(limit, MAX_CLAIM), lease_seconds


def check_finish_args(ok: bool, error: str | None, retry_in: float | None) -> tuple[str, str | None, float | None]:
    """(status, error, retry_in) for finish_effect: sent, pending again (a retry) or dead."""
    if error is not None:
        error = str(error).replace("\x00", "")[:MAX_ERROR_CHARS]
    if ok:
        return "sent", error, None
    if retry_in is not None:
        retry_in = float(retry_in)
        if not math.isfinite(retry_in) or retry_in < 0:
            raise ValueError("retry_in must be a number of seconds, at least 0")
        return "pending", error, retry_in
    return "dead", error, None


class Turn:
    """One turn's access to one conversation: `state` is a private copy; `commit()` stores it,
    together with any effects, in one transaction."""

    __slots__ = ("state", "committed", "_commit")

    def __init__(self, state: SessionState, commit: Callable[[SessionState, list[Effect]], None]) -> None:
        self.state = state
        self.committed = False
        self._commit = commit

    def commit(self, effects: Iterable[Mapping[str, Any]] = ()) -> None:
        """Write the state, and queue `effects` ({"kind", "key", "payload"} each) atomically
        with it. A key the conversation has queued before is ignored, so a retried turn cannot
        queue the same action twice."""
        if self.committed:
            raise StoreError("this turn has already been committed")
        queued = normalize_effects(effects)
        self._commit(self.state, queued)
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

    # -- conversations ------------------------------------------------------------------
    @abstractmethod
    def create(self, state: SessionState) -> None: ...

    @abstractmethod
    def read(self, session_id: str) -> SessionState: ...

    @abstractmethod
    def turn(self, session_id: str, wait: float | None = None) -> Any:
        """Context manager yielding a Turn; see the package docstring."""

    # -- audit trail and outbox ---------------------------------------------------------
    @abstractmethod
    def audit(self, session_id: str) -> list[dict[str, Any]]:
        """The conversation's audit events, oldest first; [] if none are known."""

    @abstractmethod
    def effects(self, session_id: str) -> list[dict[str, Any]]:
        """The conversation's queued effects without their payloads, oldest first:
        {id, kind, key, status, attempts, created_at, sent_at, last_error}."""

    @abstractmethod
    def claim_effects(self, limit: int = 10, lease_seconds: float = 30.0) -> list[dict[str, Any]]:
        """Up to `limit` pending effects that are due, each claimed for this caller alone for
        `lease_seconds`: {id, session_id, kind, key, payload, attempts, token}. A claim that
        lapses makes the effect due again."""

    @abstractmethod
    def finish_effect(
        self,
        effect_id: int,
        token: str,
        *,
        ok: bool,
        error: str | None = None,
        retry_in: float | None = None,
    ) -> bool:
        """Record the outcome of a claimed effect: sent; or pending again, due in `retry_in`
        seconds; or (not ok, no retry) dead. False, and nothing written, if the claim was lost."""

    # -- operations -----------------------------------------------------------------------
    @abstractmethod
    def stats(self) -> dict[str, Any]: ...

    @abstractmethod
    def ping(self) -> None:
        """A cheap check that the store answers; StoreUnavailable if it does not."""

    def describe(self) -> str:
        """One line for logs: the kind of store and where it is (never a password)."""
        return self.kind

    def renew_leases(self) -> int:
        """Renew the holds of the turns in progress now (stores with leases do this on their own
        about every lease/3). Returns how many were renewed."""
        return 0

    def sweep(self) -> dict[str, int]:
        """One round of housekeeping now (stores do this on their own about once a minute)."""
        return {}

    def close(self) -> None:
        """Release OS resources. The store stays usable (it reconnects when used again)."""
