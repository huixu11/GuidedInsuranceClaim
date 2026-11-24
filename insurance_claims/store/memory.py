"""MemoryStore: one process, the development default.

Ordered by last use, so expiry and eviction are O(1); idle conversations expire after a TTL
(checked on read as well as on write); at most `max_sessions` are held, and at most `max_bytes`
of state, and beyond either the least recently used is evicted (it then answers 404, which the UI
handles by starting afresh). The state is kept as the JSON text of `SessionState.to_record()`, so
every read and every turn works on a fresh copy and the serialisation is exercised on every
request.

The audit trail and the outbox live with the conversation here and go when it goes: this store is
for one process in development, not for keeping records.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from ..state import SessionState
from .base import (
    SPLIT_FIELDS,
    ConversationBusy,
    Effect,
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


class _Entry:
    __slots__ = ("record", "used", "version", "busy", "waiters", "cond", "effects")

    def __init__(self, record: str, used: float) -> None:
        self.record = record
        self.used = used  # clock time of the last use
        self.version = 1
        self.busy = False  # a turn holds the conversation
        self.waiters = 0
        self.cond: threading.Condition | None = None
        self.effects: dict[str, int] = {}  # effect key -> effect id


class _Effect:
    __slots__ = (
        "id", "session_id", "kind", "key", "payload", "status", "attempts", "next_attempt_at",
        "claimed_by", "claimed_until", "created_at", "sent_at", "last_error",
    )

    def __init__(self, effect_id: int, session_id: str, effect: Effect, now: float) -> None:
        self.id = effect_id
        self.session_id = session_id
        self.kind, self.key, self.payload = effect.kind, effect.key, effect.payload
        self.status = "pending"
        self.attempts = 0
        self.next_attempt_at = now
        self.claimed_by: str | None = None
        self.claimed_until: float | None = None
        self.created_at = now
        self.sent_at: float | None = None
        self.last_error: str | None = None


class MemoryStore(SessionStore):
    kind = "memory"
    shared = False

    def __init__(
        self,
        ttl_seconds: float = 4 * 3600,
        max_sessions: int = 50_000,
        wait_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
        max_bytes: int = 0,
    ) -> None:
        super().__init__(ttl_seconds, max_sessions, wait_seconds)
        # The state held, as JSON text, is the process's main use of memory: a short conversation
        # is ~20 KB, one at the message cap up to ~400 KB. 0 = bounded by max_sessions alone.
        self.max_bytes = max(0, int(max_bytes))
        self._clock = clock or time.monotonic  # last use: expiry and eviction order
        self._time = clock or time.time  # effect times (epoch seconds, as the SQL stores report)
        self._lock = threading.Lock()
        # Least recently used first: every use moves the entry to the end.
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._busy = 0
        self._effects: dict[int, _Effect] = {}  # in id order
        self._next_effect_id = 1

    # -- internals (the caller holds self._lock) ------------------------------------
    def _drop(self, session_id: str, entry: _Entry) -> None:
        del self._entries[session_id]
        self._bytes -= len(entry.record)
        for effect_id in entry.effects.values():
            self._effects.pop(effect_id, None)
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

    def _sweep(self) -> int:
        """Drop expired conversations from the old end; stops at the first live one."""
        now = self._clock()
        dropped = 0
        for _ in range(len(self._entries)):
            session_id, entry = next(iter(self._entries.items()))
            if entry.busy:  # mid-turn: in use, so it belongs at the recent end
                self._entries.move_to_end(session_id)
                continue
            if now - entry.used <= self.ttl:
                break
            self._drop(session_id, entry)
            dropped += 1
        self._count("expired", dropped)
        return dropped

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

    # -- conversations ----------------------------------------------------------------
    def create(self, state: SessionState) -> None:
        try:
            record = dumps(state.to_record())
        except (TypeError, ValueError) as exc:
            raise StoreError(f"conversation {state.session_id[:8]} could not be serialised: {exc}") from exc
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
        return state_from_record(json.loads(record), session_id)

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
            state = state_from_record(json.loads(record), session_id)
            loaded = json.loads(record)  # a second copy: what the append-only lists held
            baseline = {name: loaded.get(name) or [] for name in SPLIT_FIELDS}
            yield Turn(state, lambda s, effects: self._commit(session_id, entry, version, baseline, s, effects))
        finally:
            with self._lock:
                entry.busy = False
                self._busy -= 1
                if entry.waiters:
                    entry.cond.notify()  # type: ignore[union-attr]
                else:
                    entry.cond = None

    def _commit(
        self,
        session_id: str,
        entry: _Entry,
        version: int,
        baseline: dict[str, list[Any]],
        state: SessionState,
        effects: list[Effect],
    ) -> None:
        if state.session_id != session_id:
            raise StoreError("a turn cannot change the conversation id")
        full = state.to_record()
        split_appended(dict(full), baseline)  # the same rules as the stores that keep rows
        try:
            record = dumps(full)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"conversation {session_id[:8]} could not be serialised: {exc}") from exc
        with self._lock:
            if self._entries.get(session_id) is not entry or entry.version != version:
                raise ConversationBusy(session_id)  # cannot happen while the entry is busy
            self._bytes += len(record) - len(entry.record)
            entry.record = record
            entry.version += 1
            now = self._time()
            for effect in effects:
                if effect.key not in entry.effects:
                    effect_id = self._next_effect_id
                    self._next_effect_id += 1
                    self._effects[effect_id] = _Effect(effect_id, session_id, effect, now)
                    entry.effects[effect.key] = effect_id
            self._touch(session_id, entry)
            self._sweep()
            self._evict()  # the state grew; this entry is mid-turn, so it is never the one dropped

    # -- audit trail and outbox -------------------------------------------------------------
    def audit(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(session_id)
            record = entry.record if entry is not None else None
        if record is None:
            return []
        return list(json.loads(record).get("events") or [])

    def effects(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(session_id)
            found = [self._effects[i] for i in sorted(entry.effects.values())] if entry is not None else []
            return [
                {
                    "id": e.id, "kind": e.kind, "key": e.key, "status": e.status, "attempts": e.attempts,
                    "created_at": e.created_at, "sent_at": e.sent_at, "last_error": e.last_error,
                }
                for e in found
            ]

    def claim_effects(self, limit: int = 10, lease_seconds: float = 30.0) -> list[dict[str, Any]]:
        limit, lease_seconds = check_claim_args(limit, lease_seconds)
        token = uuid.uuid4().hex
        with self._lock:
            now = self._time()
            due = [e for e in self._effects.values() if e.status == "pending" and e.next_attempt_at <= now]
            due.sort(key=lambda e: (e.next_attempt_at, e.id))
            claimed = []
            for e in due[:limit]:
                e.claimed_by = token
                e.claimed_until = e.next_attempt_at = now + lease_seconds
                e.attempts += 1
                claimed.append(
                    {
                        "id": e.id, "session_id": e.session_id, "kind": e.kind, "key": e.key,
                        "payload": json.loads(e.payload), "attempts": e.attempts, "token": token,
                    }
                )
        return sorted(claimed, key=lambda c: c["id"])

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
        with self._lock:
            e = self._effects.get(effect_id)
            if e is None or e.claimed_by != token or e.status not in ("pending", status):
                return False
            now = self._time()
            if status == "sent":
                e.sent_at = e.sent_at or now
            elif status == "pending":
                e.next_attempt_at = now + (retry_in or 0.0)
            if status != "sent":
                e.last_error = error
            e.status = status
            e.claimed_until = None
            return True

    # -- operations -------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._sweep()
            held = len(self._entries)
            effects = {s: 0 for s in ("pending", "sent", "dead")}
            for e in self._effects.values():
                effects[e.status] += 1
            return {
                "kind": self.kind,
                "shared_between_processes": False,
                "sessions_held": held,
                "turns_in_progress": self._busy,
                "max_sessions": self.max_sessions,
                "ttl_seconds": self.ttl,
                "state_bytes_held": self._bytes,
                "max_state_bytes": self.max_bytes or None,
                "effects": effects,
                "evicted_since_start": self.evicted,
                "expired_since_start": self.expired,
            }

    def ping(self) -> None:
        return None

    def describe(self) -> str:
        return "memory (this process only)"

    def sweep(self) -> dict[str, int]:
        with self._lock:
            return {"conversations": self._sweep()}
