"""Delivery of what a turn decided to send: the summary e-mail and the handoff to a human.

A turn never talks to the outside world itself. What it decided is derived from the change it
made to the conversation (`turn_effects`) and queued in the store's outbox in the same
transaction as the conversation (`Turn.commit(effects=...)`): a crash can neither lose an effect
of a committed turn nor deliver one of a turn that was not committed. A `Dispatcher` then claims
due effects, delivers each through the transport for its kind and records the outcome:

    sent        delivered;
    retried     a temporary failure (no connection, a timeout, 408/429/5xx, an SMTP 4xx): due
                again after 2 s, 4 s, 8 s ... (at most 5 min, with jitter); after
                SOP_DISPATCH_MAX_ATTEMPTS attempts it is set aside as dead;
    dead        a permanent failure (unknown kind, malformed payload, no address on file, any
                other 4xx, an SMTP 5xx): set aside at once, never retried.

Delivery is at least once: a dispatcher can die after delivering and before recording it; the
claim then lapses and the effect is delivered again. So every delivery carries a stable
idempotency key for the receiver to recognise a repeat (`delivery_key`: a digest of the
conversation id and the effect's key): the Message-ID of an e-mail (<digest@sender's domain>) and
the Idempotency-Key header of the webhook. It is a digest because the conversation id is what
lets its holder read the conversation back, and an e-mail header ends up in the recipient's
mailbox and in every mail server on the way. The webhook's body does name the conversation
(`session_id`): the agent desk is the operator's own system and needs it to open the transcript.

Effects (key unique per conversation; the store ignores a key queued twice):

    email    email-<n>   the n-th entry of state.outbox
             payload {party_id, to_masked, subject, body, turn}: no address. The recipient is
             the address on file of that party, looked up when the e-mail is delivered.
    handoff  handoff     the conversation was handed to a human
             payload {reason, turn, summary, phase, verified, party_id, case_id}

Transports (settings, see config.py):

    SOP_EMAIL_TRANSPORT    demo (default: one log line, nothing sent) | smtp
                           (SOP_SMTP_URL smtp://user:password@host:port or smtps://...,
                           STARTTLS when offered; SOP_EMAIL_FROM; SOP_EMAIL_REDIRECT_TO sends
                           every message to that one address, the intended recipient masked in
                           a header: the fixture addresses are fictitious)
    SOP_HANDOFF_TRANSPORT  demo (default) | webhook (SOP_HANDOFF_WEBHOOK_URL, optional
                           SOP_HANDOFF_WEBHOOK_TOKEN sent as a Bearer token): POST of JSON

Every server process runs one dispatcher (SOP_DISPATCH=off: none); `python -m
insurance_claims.dispatch` runs a dispatcher alone, for deployments that keep delivery apart
from the web instances. Any number of dispatchers can share one store: a claim hands each due
effect to one of them at a time. Error texts recorded with an effect (and shown by the audit
endpoint) and log lines carry codes and counts only: no address, no content, no secret.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import http.client
import json
import logging
import math
import random
import signal
import smtplib
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from .config import Settings, load_settings
from .data import Repository
from .identity import mask_email
from .state import SessionState
from .store import SessionStore, StoreError, open_store, parse_store_spec
from .tools import summary_recipient

log = logging.getLogger("sop.dispatch")

EMAIL = "email"
HANDOFF = "handoff"
HANDOFF_KEY = "handoff"
TIMEOUT_SECONDS = 10.0  # one network operation of a delivery
BACKOFF_FIRST_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0
CLAIM_BATCH = 10
CLAIM_LEASE_SECONDS = 120.0  # a batch still running after half of this hands back what is left
QUIET_SECONDS = 60.0  # a failing store is logged at most this often

# The outbox's statuses as the API shows them.
DELIVERY_STATUS = {"pending": "queued", "sent": "sent", "dead": "failed"}
QUEUED = {"status": "queued", "attempts": 0, "sent_at": None}


def email_key(index: int) -> str:
    return f"email-{index}"


def delivery_key(session_id: str, key: str) -> str:
    """The idempotency key a delivery carries outside: the same for every attempt at one effect,
    different for every other effect, and not the conversation id (which would let whoever sees
    an e-mail header read the conversation)."""
    return hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# What a turn queues, and how the API shows it
# ---------------------------------------------------------------------------
def turn_effects(state: SessionState, outbox_before: int, had_escalation: bool) -> list[dict[str, Any]]:
    """The effects of one turn, from what it changed: each entry it added to `state.outbox`
    (which held `outbox_before` entries when the turn started), and the handoff if the turn
    escalated the conversation."""
    effects: list[dict[str, Any]] = []
    for index in range(outbox_before, len(state.outbox)):
        entry = state.outbox[index]
        effects.append(
            {
                "kind": EMAIL,
                "key": email_key(index),
                "payload": {
                    "party_id": state.party_id,
                    "to_masked": entry.get("to"),
                    "subject": entry.get("subject"),
                    "body": entry.get("body"),
                    "turn": entry.get("sent_turn"),
                },
            }
        )
    if state.escalation is not None and not had_escalation:
        escalation = state.escalation
        effects.append(
            {
                "kind": HANDOFF,
                "key": HANDOFF_KEY,
                "payload": {
                    "reason": escalation.get("reason"),
                    "turn": escalation.get("turn"),
                    "summary": escalation.get("handoff_summary"),
                    "phase": state.phase.value,
                    "verified": state.verified,
                    "party_id": state.party_id,
                    "case_id": state.selected_case_id,
                },
            }
        )
    return effects


def delivery_of(effect: Mapping[str, Any]) -> dict[str, Any]:
    """{status: queued | sent | failed, attempts, sent_at} for one entry of store.effects()."""
    return {
        "status": DELIVERY_STATUS.get(str(effect.get("status")), "queued"),
        "attempts": effect.get("attempts", 0),
        "sent_at": effect.get("sent_at"),
    }


def with_delivery(view: dict[str, Any], deliveries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The state view (`SessionState.to_view`) with a `delivery` object on each outbox entry
    and on the escalation that `deliveries` (effect key -> delivery) knows. The view's lists
    and dicts are the state's own, so they are copied, never changed in place."""
    if not deliveries:
        return view
    out = dict(view)
    email_view = view.get("email")
    if isinstance(email_view, dict) and email_view.get("outbox"):
        outbox = []
        for index, entry in enumerate(email_view["outbox"]):
            known = deliveries.get(email_key(index))
            outbox.append({**entry, "delivery": dict(known)} if known is not None and isinstance(entry, dict) else entry)
        out["email"] = {**email_view, "outbox": outbox}
    escalation = view.get("escalation")
    known = deliveries.get(HANDOFF_KEY)
    if isinstance(escalation, dict) and known is not None:
        out["escalation"] = {**escalation, "delivery": dict(known)}
    return out


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------
class DeliveryError(Exception):
    """A delivery failed. `permanent`: trying again cannot help (dead at once); otherwise it is
    retried with backoff, not sooner than `retry_after` seconds if the receiver asked for that.
    The text is stored with the effect and shown by the audit endpoint, so it is a short code
    (a status, an exception type), never an address, content or a secret."""

    def __init__(self, reason: str, *, permanent: bool, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent
        self.retry_after = retry_after


@dataclass(frozen=True)
class EmailJob:
    recipient: str  # the address on file
    to_masked: str
    subject: str
    body: str
    idempotency_key: str  # delivery_key(session_id, key)
    ref: str  # for log lines: "<first 8 characters of the conversation id>:<key>"


@dataclass(frozen=True)
class HandoffJob:
    session_id: str
    idempotency_key: str
    ref: str
    handoff: dict[str, Any]


class EmailTransport(Protocol):
    name: str

    def send(self, job: EmailJob) -> None: ...


class HandoffTransport(Protocol):
    name: str

    def send(self, job: HandoffJob) -> None: ...


def _ref(session_id: str, key: str) -> str:
    """An effect as a log line names it: the conversation id cut to 8 characters, as in every
    other log line (the full id is what lets anyone read the conversation back)."""
    return f"{session_id[:8]}:{key}"


class DemoEmail:
    """No network: one log line (no address, no content), and the e-mail counts as sent."""

    name = "demo"

    def send(self, job: EmailJob) -> None:
        log.info("demo e-mail transport: %s delivered (nothing is sent anywhere)", job.ref)

    def __repr__(self) -> str:
        return "<DemoEmail>"


class DemoHandoff:
    name = "demo"

    def send(self, job: HandoffJob) -> None:
        log.info("demo handoff transport: %s handed to a human (nothing is sent anywhere)", job.ref)

    def __repr__(self) -> str:
        return "<DemoHandoff>"


SMTP_URL_HELP = "SOP_SMTP_URL must look like smtp://user:password@host:port or smtps://user:password@host:port"


def parse_smtp_url(url: str) -> tuple[bool, str, int, str, str]:
    """(implicit TLS, host, port, user, password) from SOP_SMTP_URL. smtp:// defaults to port
    587 (STARTTLS when the server offers it), smtps:// to 465. Errors never repeat the URL."""
    try:
        parts = urlsplit((url or "").strip())
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
        user, password = unquote(parts.username or ""), unquote(parts.password or "")
    except ValueError:
        raise ValueError(SMTP_URL_HELP) from None
    if scheme not in ("smtp", "smtps") or not host:
        raise ValueError(SMTP_URL_HELP)
    implicit_tls = scheme == "smtps"
    return implicit_tls, host, port or (465 if implicit_tls else 587), user, password


def _domain_of(address: str) -> str:
    domain = email.utils.parseaddr(address)[1].rpartition("@")[2].strip()
    return domain if domain and all(c.isalnum() or c in ".-" for c in domain) else "localhost"


def message_id(idempotency_key: str, sender: str) -> str:
    """A stable Message-ID for one effect: <its delivery key@the sender's domain>."""
    return f"<{idempotency_key}@{_domain_of(sender)}>"


class SmtpEmail:
    """smtplib, 10 s per network operation, STARTTLS when offered (smtps://: TLS from the
    start). A password is only ever sent over an encrypted connection."""

    name = "smtp"

    def __init__(self, url: str, sender: str, redirect_to: str = "", timeout: float = TIMEOUT_SECONDS) -> None:
        self._tls, self.host, self.port, self._user, self._password = parse_smtp_url(url)
        if not sender or "@" not in sender:
            raise ValueError("SOP_EMAIL_TRANSPORT=smtp needs SOP_EMAIL_FROM, the sender's address")
        if redirect_to and "@" not in redirect_to:
            raise ValueError("SOP_EMAIL_REDIRECT_TO must be an e-mail address")
        self.sender = sender
        self.redirect_to = redirect_to
        self.timeout = float(timeout)

    def __repr__(self) -> str:
        scheme = "smtps" if self._tls else "smtp"
        return f"<SmtpEmail {scheme}://{self.host}:{self.port}" + (" redirected" if self.redirect_to else "") + ">"

    def build(self, job: EmailJob) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = self.redirect_to or job.recipient
        if self.redirect_to:
            msg["X-Intended-Recipient"] = job.to_masked  # masked: the real address never travels
        msg["Subject"] = " ".join(job.subject.split())  # no line breaks in a header
        msg["Date"] = email.utils.formatdate(usegmt=True)
        msg["Message-ID"] = message_id(job.idempotency_key, self.sender)
        # Quoted-printable for text that is not plain ASCII: 7-bit clean for any server, without
        # relying on its 8BITMIME support.
        msg.set_content(job.body, cte=None if job.body.isascii() else "quoted-printable")
        return msg

    def send(self, job: EmailJob) -> None:
        try:
            msg = self.build(job)
        except (ValueError, TypeError) as exc:
            raise DeliveryError(f"malformed e-mail ({type(exc).__name__})", permanent=True) from None
        to = self.redirect_to or job.recipient
        context = ssl.create_default_context()
        smtp: smtplib.SMTP | None = None
        try:
            if self._tls:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            smtp.ehlo()
            encrypted = self._tls
            if not encrypted and smtp.has_extn("starttls"):
                smtp.starttls(context=context)
                smtp.ehlo()
                encrypted = True
            if self._user:
                if not encrypted:
                    raise DeliveryError("smtp: the server offers no TLS; the password is not sent in clear", permanent=True)
                smtp.login(self._user, self._password)
            smtp.send_message(msg, from_addr=email.utils.parseaddr(self.sender)[1], to_addrs=[to])
        except DeliveryError:
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            codes = sorted({int(code) for code, _ in exc.recipients.values()})
            raise DeliveryError(
                f"smtp: recipient refused ({', '.join(map(str, codes))})",
                permanent=bool(codes) and all(500 <= c < 600 for c in codes),
            ) from None
        except smtplib.SMTPResponseException as exc:  # authentication, sender, data: by the code
            code = int(exc.smtp_code)
            raise DeliveryError(f"smtp: {code}", permanent=500 <= code < 600) from None
        except smtplib.SMTPNotSupportedError:
            raise DeliveryError("smtp: not supported by the server", permanent=True) from None
        except ssl.SSLCertVerificationError:
            raise DeliveryError("smtp: the server's TLS certificate is not trusted", permanent=True) from None
        except TimeoutError:
            raise DeliveryError("smtp: timed out", permanent=False) from None
        except (OSError, smtplib.SMTPException) as exc:  # refused, reset, disconnected, ...
            raise DeliveryError(f"smtp: connection failed ({type(exc).__name__})", permanent=False) from None
        finally:
            if smtp is not None:
                try:
                    smtp.quit()  # the message is accepted by now; a failed goodbye changes nothing
                except (OSError, smtplib.SMTPException):
                    smtp.close()


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A webhook that answers with a redirect is misconfigured: a POST is never re-sent elsewhere
    (urllib would turn it into a GET)."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _retry_after_seconds(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds > 0 else None


class WebhookHandoff:
    """POST of JSON to SOP_HANDOFF_WEBHOOK_URL, 10 s timeout, standard library only. Headers:
    Idempotency-Key, and Authorization: Bearer <SOP_HANDOFF_WEBHOOK_TOKEN> when one is set."""

    name = "webhook"

    def __init__(self, url: str, token: str = "", timeout: float = TIMEOUT_SECONDS) -> None:
        try:
            parts = urlsplit((url or "").strip())
            parts.port  # noqa: B018 - raises ValueError for a port that is not a number
        except ValueError:
            parts = None
        if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("SOP_HANDOFF_TRANSPORT=webhook needs SOP_HANDOFF_WEBHOOK_URL, an http:// or https:// URL")
        if any(c in token for c in "\r\n"):
            raise ValueError("SOP_HANDOFF_WEBHOOK_TOKEN must be one line")
        self._url = url.strip()
        self._token = token.strip()
        self._where = f"{parts.scheme.lower()}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        self.timeout = float(timeout)
        self._opener = urllib.request.build_opener(_NoRedirects)

    def __repr__(self) -> str:  # no path or query (either may carry a secret), no token
        return f"<WebhookHandoff {self._where}>"

    def send(self, job: HandoffJob) -> None:
        body = json.dumps(
            {
                "event": "handoff",
                "idempotency_key": job.idempotency_key,
                "session_id": job.session_id,
                "handoff": job.handoff,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": job.idempotency_key,
            "User-Agent": "sop-claims-agent-dispatcher",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                response.read(65536)
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
            temporary = status in (408, 429) or status >= 500
            raise DeliveryError(f"webhook: HTTP {status}", permanent=not temporary, retry_after=retry_after) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise DeliveryError("webhook: the server's TLS certificate is not trusted", permanent=True) from None
            if isinstance(reason, TimeoutError):
                raise DeliveryError("webhook: timed out", permanent=False) from None
            raise DeliveryError(f"webhook: connection failed ({type(reason).__name__})", permanent=False) from None
        except TimeoutError:
            raise DeliveryError("webhook: timed out", permanent=False) from None
        except (OSError, http.client.HTTPException) as exc:
            raise DeliveryError(f"webhook: connection failed ({type(exc).__name__})", permanent=False) from None
        if not 200 <= status < 300:
            raise DeliveryError(f"webhook: HTTP {status}", permanent=True)


def build_transports(settings: Settings) -> tuple[EmailTransport, HandoffTransport]:
    """The transports the settings ask for. A ValueError (whose message names the setting and
    never contains a secret) for a misconfiguration: the process does not start with one."""
    kind = (settings.email_transport or "demo").strip().lower()
    if kind == "demo":
        mail: EmailTransport = DemoEmail()
    elif kind == "smtp":
        if not settings.smtp_url:
            raise ValueError("SOP_EMAIL_TRANSPORT=smtp needs SOP_SMTP_URL: smtp://user:password@host:port or smtps://...")
        mail = SmtpEmail(settings.smtp_url, settings.email_from, settings.email_redirect_to)
    else:
        raise ValueError(f"SOP_EMAIL_TRANSPORT must be demo or smtp, not {kind[:20]!r}")
    kind = (settings.handoff_transport or "demo").strip().lower()
    if kind == "demo":
        handoff: HandoffTransport = DemoHandoff()
    elif kind == "webhook":
        handoff = WebhookHandoff(settings.handoff_webhook_url, settings.handoff_webhook_token)
    else:
        raise ValueError(f"SOP_HANDOFF_TRANSPORT must be demo or webhook, not {kind[:20]!r}")
    return mail, handoff


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------
def backoff_seconds(attempts: int, retry_after: float | None = None) -> float:
    """2 s after the first failed attempt, then 4, 8 ... at most 5 minutes, +-20 % jitter so
    that effects that failed together do not all come back together; never sooner than the
    receiver's Retry-After."""
    base = BACKOFF_FIRST_SECONDS * 2.0 ** max(0, min(attempts, 30) - 1)
    delay = min(BACKOFF_MAX_SECONDS, base) * random.uniform(0.8, 1.2)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(BACKOFF_MAX_SECONDS, delay)


class Dispatcher:
    """Delivers due effects of one store. `run_once()` is one round (claim a batch, deliver it,
    record each outcome); `start()` runs rounds on a daemon thread until `stop()`."""

    def __init__(
        self,
        store: SessionStore,
        repo: Repository,
        settings: Settings,
        *,
        email: EmailTransport | None = None,
        handoff: HandoffTransport | None = None,
        batch: int = CLAIM_BATCH,
        lease_seconds: float = CLAIM_LEASE_SECONDS,
    ) -> None:
        if email is None or handoff is None:
            built_email, built_handoff = build_transports(settings)
            email = email or built_email
            handoff = handoff or built_handoff
        self.store = store
        self.repo = repo
        self.email = email
        self.handoff = handoff
        self.max_attempts = max(1, int(settings.dispatch_max_attempts))
        self.poll_seconds = max(0.01, float(settings.dispatch_poll_seconds))
        self.batch = max(1, int(batch))
        self.lease_seconds = float(lease_seconds)
        self._lock = threading.Lock()
        self._counts = {"rounds": 0, "delivered": 0, "retried": 0, "dead": 0, "handed_back": 0, "store_errors": 0}
        self._last_error_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._quiet_until = 0.0

    def __repr__(self) -> str:
        return f"<Dispatcher {self.store.kind} email={self.email!r} handoff={self.handoff!r}>"

    # -- one round ------------------------------------------------------------------------
    def run_once(self) -> int:
        """Claim up to `batch` due effects, deliver them and record the outcomes. Returns how
        many were claimed. A StoreError from the claim itself is raised; nothing else is."""
        claimed = self.store.claim_effects(limit=self.batch, lease_seconds=self.lease_seconds)
        self._count("rounds")
        started = time.monotonic()
        for i, effect in enumerate(claimed):
            if time.monotonic() - started > self.lease_seconds / 2:
                # Slow deliveries ate into the claim: hand the rest back (due at once) rather
                # than deliver them after the claim could have lapsed to another dispatcher.
                for rest in claimed[i:]:
                    self._record(rest, ok=False, retry_in=0.0)
                    self._count("handed_back")
                break
            self._deliver(effect)
        return len(claimed)

    def _deliver(self, effect: dict[str, Any]) -> None:
        ref = _ref(str(effect["session_id"]), str(effect["key"]))
        attempts = int(effect.get("attempts") or 1)
        try:
            self._send(effect, ref)
        except DeliveryError as exc:
            self._failed(effect, ref, attempts, exc)
            return
        except Exception as exc:  # noqa: BLE001 - a bug in a transport must not stop the loop
            log.exception("delivering effect %s (%s) failed unexpectedly", effect.get("id"), ref)
            self._failed(effect, ref, attempts, DeliveryError(f"unexpected error ({type(exc).__name__})", permanent=False))
            return
        self._count("delivered")
        self._record(effect, ok=True)

    def _send(self, effect: dict[str, Any], ref: str) -> None:
        kind, payload = effect.get("kind"), effect.get("payload")
        if not isinstance(payload, dict):
            raise DeliveryError("malformed payload (not an object)", permanent=True)
        key = delivery_key(str(effect["session_id"]), str(effect["key"]))
        if kind == EMAIL:
            self.email.send(self._email_job(payload, key, ref))
        elif kind == HANDOFF:
            self.handoff.send(self._handoff_job(effect["session_id"], payload, key, ref))
        else:
            raise DeliveryError(f"unknown kind {str(kind)[:40]!r}", permanent=True)

    def _email_job(self, payload: dict[str, Any], key: str, ref: str) -> EmailJob:
        subject, body, party_id = payload.get("subject"), payload.get("body"), payload.get("party_id")
        if not isinstance(subject, str) or not subject.strip() or not isinstance(body, str):
            raise DeliveryError("malformed e-mail payload (subject and body are required)", permanent=True)
        address = summary_recipient(self.repo, party_id if isinstance(party_id, str) else None)
        if address is None:
            raise DeliveryError("no address on file", permanent=True)
        masked = payload.get("to_masked")
        return EmailJob(
            recipient=address,
            to_masked=masked if isinstance(masked, str) and masked else mask_email(address),
            subject=subject,
            body=body,
            idempotency_key=key,
            ref=ref,
        )

    @staticmethod
    def _handoff_job(session_id: str, payload: dict[str, Any], key: str, ref: str) -> HandoffJob:
        if not isinstance(payload.get("reason"), str) or not payload["reason"] or not isinstance(payload.get("summary"), str):
            raise DeliveryError("malformed handoff payload (reason and summary are required)", permanent=True)
        return HandoffJob(session_id=session_id, idempotency_key=key, ref=ref, handoff=dict(payload))

    def _failed(self, effect: dict[str, Any], ref: str, attempts: int, exc: DeliveryError) -> None:
        with self._lock:
            self._last_error_at = time.time()
        if exc.permanent or attempts >= self.max_attempts:
            self._count("dead")
            why = "permanent failure" if exc.permanent else f"still failing after {attempts} attempt{'s' * (attempts != 1)}"
            log.warning("effect %s (%s) set aside, %s: %s", effect.get("id"), ref, why, exc.reason)
            self._record(effect, ok=False, error=exc.reason)
            return
        delay = backoff_seconds(attempts, exc.retry_after)
        self._count("retried")
        log.info("effect %s (%s) failed (attempt %d): %s; trying again in %.0f s",
                 effect.get("id"), ref, attempts, exc.reason, delay)
        self._record(effect, ok=False, error=exc.reason, retry_in=delay)

    def _record(self, effect: dict[str, Any], *, ok: bool, error: str | None = None, retry_in: float | None = None) -> None:
        try:
            recorded = self.store.finish_effect(effect["id"], effect["token"], ok=ok, error=error, retry_in=retry_in)
        except StoreError as exc:
            # Not recorded: the claim lapses and the effect is delivered again (at least once).
            self._count("store_errors")
            self._quiet_warning("the outcome of effect %s could not be recorded (%s); it is delivered again "
                                "once its claim lapses", effect.get("id"), exc)
            return
        if not recorded:
            log.warning("effect %s: its claim lapsed before the outcome was recorded; another dispatcher "
                        "may deliver it again", effect.get("id"))

    # -- the thread -------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, args=(self._stop,), name="sop-dispatcher", daemon=True)
            self._thread.start()
        log.info("dispatcher started: e-mail %s, handoff %s, every %g s", self.email.name, self.handoff.name,
                 self.poll_seconds)

    def stop(self, timeout: float = 15.0) -> bool:
        """Stop after the delivery in progress (if any); True once the thread has ended."""
        with self._lock:
            thread, stop = self._thread, self._stop
        stop.set()
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                found = self.run_once()
            except StoreError as exc:
                found = 0
                self._count("store_errors")
                with self._lock:
                    self._last_error_at = time.time()
                self._quiet_warning("the dispatcher cannot claim effects (%s); trying again every %g s",
                                    exc, self.poll_seconds)
            except Exception:  # noqa: BLE001 - the thread must outlive any one failure
                found = 0
                if self._may_log():
                    log.exception("a dispatcher round failed")
            if not found:  # nothing was due: wait; otherwise go again at once
                stop.wait(self.poll_seconds)

    # -- counters and logs ---------------------------------------------------------------------
    def _count(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def _may_log(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now < self._quiet_until:
                return False
            self._quiet_until = now + QUIET_SECONDS
            return True

    def _quiet_warning(self, message: str, *args: Any) -> None:
        if self._may_log():
            log.warning(message, *args)

    def snapshot(self) -> dict[str, Any]:
        """For /api/stats: counts since start and the time of the last failure. No content."""
        with self._lock:
            counts = dict(self._counts)
            last_error_at = self._last_error_at
        return {
            "enabled": True,
            "running": self.running,
            "email_transport": self.email.name,
            "handoff_transport": self.handoff.name,
            "poll_seconds": self.poll_seconds,
            "max_attempts": self.max_attempts,
            "scope": "this process since it started",
            **counts,
            "last_error_at": last_error_at,
        }


# ---------------------------------------------------------------------------
# python -m insurance_claims.dispatch
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m insurance_claims.dispatch",
        description="Deliver queued e-mails and handoffs from a shared session store, without serving HTTP.",
    )
    parser.add_argument("--once", action="store_true", help="deliver what is due now, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s", stream=sys.stderr)
    try:
        settings = load_settings()
        kind, _ = parse_store_spec(settings.session_store)
        build_transports(settings)
    except ValueError as exc:
        parser.error(str(exc))
    if kind == "memory":
        parser.error(
            "SOP_SESSION_STORE is the in-memory store, which only the server process itself can see: "
            "a separate dispatcher needs sqlite:///path/sessions.db or postgresql://... (or leave delivery "
            "to the server, SOP_DISPATCH=on)"
        )
    store = open_store(settings)
    dispatcher = Dispatcher(store, Repository.load(), settings)
    try:
        if args.once:
            while dispatcher.run_once():
                pass
            return 0
        stop = threading.Event()

        def on_signal(signum: int, _frame: Any) -> None:
            log.info("signal %s: stopping after the delivery in progress", signum)
            stop.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), on_signal)
        log.info("delivering from %s", store.describe())
        dispatcher.start()
        while not stop.wait(1.0):
            pass
        dispatcher.stop()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
