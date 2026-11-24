"""Delivery of queued effects (dispatch.py) against real local receivers: an SMTP server and a
webhook endpoint (tests/sinks.py). Every store: delivered once and marked sent; retried with
backoff after a temporary failure; set aside after the last attempt or at once after a permanent
failure; delivered again after a claim lapses; two dispatchers on one store; a store outage.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from insurance_claims import config, dispatch
from insurance_claims.config import Settings
from insurance_claims.dispatch import (
    DemoEmail,
    DemoHandoff,
    Dispatcher,
    SmtpEmail,
    WebhookHandoff,
    backoff_seconds,
    build_transports,
    delivery_key,
    delivery_of,
    turn_effects,
)
from insurance_claims.state import SessionState
from insurance_claims.store import StoreUnavailable

from .sinks import SmtpSink, WebhookSink

SENDER = "Harbor Mutual Claims <claims@harbor.test>"
MASKED = "m•••@email.com"


@pytest.fixture()
def webhook():
    sink = WebhookSink()
    yield sink
    sink.close()


@pytest.fixture()
def smtp():
    sink = SmtpSink()
    yield sink
    sink.close()


class Recorder:
    """A transport for both kinds that only records the idempotency key of each delivery."""

    name = "recorder"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.keys: list[str] = []
        self.lock = threading.Lock()

    def send(self, job: Any) -> None:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.keys.append(job.idempotency_key)


def conversation_with(store: Any, *effects: dict[str, Any]) -> str:
    """A new conversation whose first turn queued `effects`; returns its id."""
    state = SessionState(session_id=uuid.uuid4().hex)
    state.messages.append({"role": "assistant", "text": "Hello", "turn": 0})
    store.create(state)
    with store.turn(state.session_id) as held:
        held.state.turn += 1
        held.commit(effects=list(effects))
    return state.session_id


def email_effect(index: int = 0, party_id: str | None = "P9", **changes: Any) -> dict[str, Any]:
    payload = {
        "party_id": party_id,
        "to_masked": MASKED,
        "subject": "Summary of your Harbor Mutual claims conversation (CL-2048)",
        "body": "Hello Margaret,\n\nHere is a summary of our conversation — and your next steps.\n",
        "turn": 4,
    }
    payload.update(changes)
    return {"kind": "email", "key": f"email-{index}", "payload": payload}


def handoff_effect(**changes: Any) -> dict[str, Any]:
    payload = {
        "reason": "caller_requested",
        "turn": 2,
        "summary": "Handoff to human representative\nReason: the caller asked for a human representative",
        "phase": "VERIFY_ID",
        "verified": False,
        "party_id": None,
        "case_id": None,
    }
    payload.update(changes)
    return {"kind": "handoff", "key": "handoff", "payload": payload}


def outcomes(store: Any, sid: str) -> dict[str, tuple[str, int, str | None]]:
    return {e["key"]: (e["status"], e["attempts"], e["last_error"]) for e in store.effects(sid)}


def wait_for(condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# What a turn queues
# ---------------------------------------------------------------------------
def test_a_turn_queues_what_it_added_and_nothing_else():
    state = SessionState(session_id="s" * 32, party_id="P9")
    state.outbox.append({"to": MASKED, "subject": "Old", "body": "old", "sent_turn": 2})
    assert turn_effects(state, outbox_before=1, had_escalation=False) == []
    state.outbox.append({"to": MASKED, "subject": "New", "body": "new", "sent_turn": 5})
    (mail,) = turn_effects(state, outbox_before=1, had_escalation=False)
    assert mail == {
        "kind": "email",
        "key": "email-1",
        "payload": {"party_id": "P9", "to_masked": MASKED, "subject": "New", "body": "new", "turn": 5},
    }  # no address: it is looked up when the e-mail is delivered
    state.escalation = {"reason": "caller_requested", "turn": 5, "handoff_summary": "Handoff ..."}
    kinds = [(e["kind"], e["key"]) for e in turn_effects(state, outbox_before=2, had_escalation=False)]
    assert kinds == [("handoff", "handoff")]
    assert turn_effects(state, outbox_before=2, had_escalation=True) == []  # handed off earlier


def test_backoff_doubles_with_jitter_up_to_five_minutes_and_respects_retry_after():
    for attempts, base in ((1, 2), (2, 4), (3, 8), (4, 16)):
        assert all(0.8 * base <= backoff_seconds(attempts) <= 1.2 * base for _ in range(50))
    assert all(backoff_seconds(30) <= 300 for _ in range(50))
    assert backoff_seconds(1, retry_after=45) == 45
    assert backoff_seconds(1, retry_after=10_000) == 300


# ---------------------------------------------------------------------------
# Delivery, every store
# ---------------------------------------------------------------------------
def test_effects_are_delivered_once_and_marked_sent(store_factory, repo, settings, smtp, webhook):
    store = store_factory()
    d = Dispatcher(store, repo, settings, email=SmtpEmail(smtp.url, SENDER),
                   handoff=WebhookHandoff(webhook.url, token="s3cret-t0ken"))
    sid = conversation_with(store, email_effect(), handoff_effect())
    assert d.run_once() == 2
    assert d.run_once() == 0  # nothing left
    assert outcomes(store, sid) == {"email-0": ("sent", 1, None), "handoff": ("sent", 1, None)}
    assert all(delivery_of(e)["status"] == "sent" and e["sent_at"] for e in store.effects(sid))

    (mail,) = smtp.messages
    address = repo.policyholder("P9").email  # the address on file, looked up at delivery
    assert mail["from"] == "claims@harbor.test" and mail["to"] == [address]
    msg = mail["message"]
    assert msg["To"] == address and msg["From"] == SENDER and "X-Intended-Recipient" not in msg
    assert msg["Message-ID"] == f"<{delivery_key(sid, 'email-0')}@harbor.test>"  # the idempotency key
    assert sid not in mail["raw"].decode("ascii")  # the conversation id is not in the e-mail
    assert msg["Subject"] == "Summary of your Harbor Mutual claims conversation (CL-2048)"
    assert "— and your next steps" in msg.get_content()
    assert mail["raw"].isascii()  # quoted-printable: no 8-bit data on the wire

    (hook,) = webhook.requests
    assert hook["path"] == "/hooks/handoff?team=claims"
    assert hook["headers"]["idempotency-key"] == delivery_key(sid, "handoff")
    assert hook["headers"]["authorization"] == "Bearer s3cret-t0ken"
    assert hook["headers"]["content-type"] == "application/json"
    assert hook["json"] == {
        "event": "handoff",
        "idempotency_key": delivery_key(sid, "handoff"),
        "session_id": sid,
        "handoff": handoff_effect()["payload"],
    }
    counts = d.snapshot()
    assert (counts["delivered"], counts["retried"], counts["dead"]) == (2, 0, 0)
    blob = json.dumps(counts)
    assert "s3cret" not in blob and address not in blob


def test_a_temporary_failure_is_retried_with_backoff_then_sent(store_factory, repo, settings, smtp, webhook):
    smtp.script = ["451 4.3.0 Try again later"]
    webhook.script = [500, "429:30"]
    store = store_factory()
    d = Dispatcher(store, repo, settings, email=SmtpEmail(smtp.url, SENDER), handoff=WebhookHandoff(webhook.url))
    sid = conversation_with(store, email_effect(), handoff_effect())
    assert d.run_once() == 2
    assert outcomes(store, sid) == {"email-0": ("pending", 1, "smtp: 451"), "handoff": ("pending", 1, "webhook: HTTP 500")}
    assert d.run_once() == 0  # not due yet: 2 s (+-20 %) after the first failure
    store.test_clock.advance(3)
    assert d.run_once() == 2  # the e-mail goes through; the webhook answers 429, Retry-After: 30
    assert outcomes(store, sid) == {"email-0": ("sent", 2, "smtp: 451"), "handoff": ("pending", 2, "webhook: HTTP 429")}
    store.test_clock.advance(10)
    assert d.run_once() == 0  # the receiver asked for 30 s
    store.test_clock.advance(25)
    assert d.run_once() == 1
    assert outcomes(store, sid)["handoff"][:2] == ("sent", 3)
    assert webhook.keys() == [delivery_key(sid, "handoff")] * 3  # the same idempotency key every time
    assert smtp.attempts == 2 and len(smtp.messages) == 1
    counts = d.snapshot()
    assert (counts["delivered"], counts["retried"], counts["dead"]) == (2, 3, 0)
    assert counts["last_error_at"] is not None


def test_an_effect_still_failing_after_the_last_attempt_is_set_aside(store_factory, repo, settings, webhook):
    webhook.script = [503] * 10
    store = store_factory()
    d = Dispatcher(store, repo, dataclasses.replace(settings, dispatch_max_attempts=3),
                   email=DemoEmail(), handoff=WebhookHandoff(webhook.url))
    sid = conversation_with(store, handoff_effect())
    for _ in range(3):
        assert d.run_once() == 1
        store.test_clock.advance(400)
    assert d.run_once() == 0
    assert outcomes(store, sid) == {"handoff": ("dead", 3, "webhook: HTTP 503")}
    (effect,) = store.effects(sid)
    assert delivery_of(effect) == {"status": "failed", "attempts": 3, "sent_at": None}
    assert len(webhook.requests) == 3 and d.snapshot()["dead"] == 1


def test_a_permanent_failure_is_set_aside_at_once(store_factory, repo, settings, smtp, webhook, caplog):
    caplog.set_level(logging.INFO, logger="sop.dispatch")
    smtp.script = ["550 5.1.1 Mailbox unavailable"]
    webhook.script = [400]
    store = store_factory()
    d = Dispatcher(store, repo, settings, email=SmtpEmail(smtp.url, SENDER), handoff=WebhookHandoff(webhook.url))
    sid = conversation_with(
        store,
        email_effect(0),  # refused by the server: 550
        email_effect(1, party_id="P404"),  # nobody on file
        email_effect(2, subject="  "),  # malformed
        handoff_effect(),  # refused by the webhook: 400
        {"kind": "fax", "key": "fax-0", "payload": {"number": "555-0100"}},  # no such kind
    )
    assert d.run_once() == 5
    store.test_clock.advance(10_000)
    assert d.run_once() == 0  # never tried again
    assert outcomes(store, sid) == {
        "email-0": ("dead", 1, "smtp: 550"),
        "email-1": ("dead", 1, "no address on file"),
        "email-2": ("dead", 1, "malformed e-mail payload (subject and body are required)"),
        "handoff": ("dead", 1, "webhook: HTTP 400"),
        "fax-0": ("dead", 1, "unknown kind 'fax'"),
    }
    assert smtp.attempts == 1 and len(webhook.requests) == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert sid not in logged and sid[:8] in logged  # log lines name a conversation by its first 8 characters
    assert repo.policyholder("P9").email not in logged and "Margaret" not in logged


@pytest.mark.parametrize("store_kind", ["memory"], indirect=True)
def test_a_redirect_from_the_webhook_is_not_followed(store_factory, repo, settings, webhook):
    webhook.script = [302]
    store = store_factory()
    d = Dispatcher(store, repo, settings, email=DemoEmail(), handoff=WebhookHandoff(webhook.url))
    sid = conversation_with(store, handoff_effect())
    d.run_once()
    assert outcomes(store, sid) == {"handoff": ("dead", 1, "webhook: HTTP 302")}
    assert len(webhook.requests) == 1


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"], indirect=True)
def test_a_webhook_that_does_not_answer_times_out_and_is_retried(store_factory, repo, settings, webhook):
    webhook.script = ["hang"]
    store = store_factory()
    d = Dispatcher(store, repo, settings, email=DemoEmail(), handoff=WebhookHandoff(webhook.url, timeout=0.5))
    sid = conversation_with(store, handoff_effect())
    started = time.monotonic()
    assert d.run_once() == 1
    assert time.monotonic() - started < 1.5  # the 0.5 s timeout, not the receiver's 2 s
    assert outcomes(store, sid) == {"handoff": ("pending", 1, "webhook: timed out")}
    store.test_clock.advance(3)
    assert d.run_once() == 1 and outcomes(store, sid)["handoff"][:2] == ("sent", 2)


@pytest.mark.parametrize("store_kind", ["memory"], indirect=True)
def test_an_unreachable_receiver_is_a_temporary_failure(store_factory, repo, settings):
    import socket

    with socket.socket() as probe:  # a port where nothing listens
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = store_factory()
    d = Dispatcher(store, repo, settings, email=SmtpEmail(f"smtp://127.0.0.1:{port}", SENDER, timeout=1),
                   handoff=WebhookHandoff(f"http://127.0.0.1:{port}/hook", timeout=1))
    sid = conversation_with(store, email_effect(), handoff_effect())
    assert d.run_once() == 2
    # Refused at once on most systems; Windows retries a refused connection until the timeout.
    statuses = outcomes(store, sid)
    assert statuses["email-0"][:2] == ("pending", 1)
    assert statuses["email-0"][2] in ("smtp: timed out", "smtp: connection failed (ConnectionRefusedError)")
    assert statuses["handoff"][:2] == ("pending", 1)
    assert statuses["handoff"][2] in ("webhook: timed out", "webhook: connection failed (ConnectionRefusedError)")


@pytest.mark.parametrize("store_kind", ["memory"], indirect=True)
def test_a_redirected_email_goes_to_the_demo_inbox_with_the_intended_recipient_masked(store_factory, repo, settings, smtp):
    store = store_factory()
    transport = SmtpEmail(smtp.url, SENDER, redirect_to="demo-inbox@harbor.test")
    d = Dispatcher(store, repo, settings, email=transport, handoff=DemoHandoff())
    sid = conversation_with(store, email_effect())
    assert d.run_once() == 1
    (mail,) = smtp.messages
    assert mail["to"] == ["demo-inbox@harbor.test"]
    msg = mail["message"]
    assert msg["To"] == "demo-inbox@harbor.test" and msg["X-Intended-Recipient"] == MASKED
    address = repo.policyholder("P9").email
    assert address.encode() not in mail["raw"]  # the real address never travels
    assert msg["Message-ID"] == f"<{delivery_key(sid, 'email-0')}@harbor.test>"


def test_a_claim_that_lapses_is_delivered_by_another_dispatcher(store_kind, store_factory, repo, settings):
    a = store_factory()
    b = a if store_kind == "memory" else store_factory(clock=a.test_clock)  # another process
    sid = conversation_with(a, email_effect())
    (stuck,) = a.claim_effects(limit=10, lease_seconds=60)  # a dispatcher that dies while delivering
    recorder = Recorder()
    other = Dispatcher(b, repo, settings, email=recorder, handoff=recorder)
    assert other.run_once() == 0  # claimed by the one that died
    a.test_clock.advance(61)
    assert other.run_once() == 1
    assert recorder.keys == [delivery_key(sid, "email-0")]
    assert outcomes(b, sid) == {"email-0": ("sent", 2, None)}
    assert a.finish_effect(stuck["id"], stuck["token"], ok=False, error="late") is False  # its outcome is not recorded
    assert outcomes(b, sid) == {"email-0": ("sent", 2, None)}


def test_two_dispatchers_on_one_store_deliver_each_effect_once(store_kind, store_factory, repo, settings):
    a = store_factory(real_time=True)
    b = a if store_kind == "memory" else store_factory(real_time=True)
    sids = [conversation_with(a, email_effect(0), email_effect(1), handoff_effect()) for _ in range(20)]
    recorder = Recorder(delay=0.002)
    fast = dataclasses.replace(settings, dispatch_poll_seconds=0.05)
    first = Dispatcher(a, repo, fast, email=recorder, handoff=recorder, batch=4)
    second = Dispatcher(b, repo, fast, email=recorder, handoff=recorder, batch=4)
    first.start()
    second.start()
    try:
        wait_for(lambda: all(e["status"] == "sent" for sid in sids for e in a.effects(sid)), timeout=60)
    finally:
        assert first.stop() and second.stop()
    expected = sorted(delivery_key(sid, key) for sid in sids for key in ("email-0", "email-1", "handoff"))
    assert sorted(recorder.keys) == expected  # each delivered exactly once
    assert first.snapshot()["delivered"] + second.snapshot()["delivered"] == len(expected)
    assert not first.running and not second.running


class Outage:
    """A store whose claims fail (StoreUnavailable) while `down` is set."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.down = threading.Event()

    def claim_effects(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if self.down.is_set():
            raise StoreUnavailable("the store is down")
        return self.store.claim_effects(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)


def test_the_dispatcher_keeps_running_through_a_store_outage(store_factory, repo, settings, caplog):
    caplog.set_level(logging.INFO, logger="sop.dispatch")
    store = Outage(store_factory(real_time=True))
    sid = conversation_with(store, email_effect())
    store.down.set()
    recorder = Recorder()
    d = Dispatcher(store, repo, dataclasses.replace(settings, dispatch_poll_seconds=0.02), email=recorder, handoff=recorder)
    d.start()
    try:
        wait_for(lambda: d.snapshot()["store_errors"] >= 5)
        assert d.running and recorder.keys == []
        store.down.clear()
        wait_for(lambda: outcomes(store, sid) == {"email-0": ("sent", 1, None)})
    finally:
        assert d.stop()
    assert recorder.keys == [delivery_key(sid, "email-0")]
    warnings = [r for r in caplog.records if r.name == "sop.dispatch" and r.levelno == logging.WARNING]
    assert len(warnings) == 1  # one line for the whole outage, not one per round
    assert "cannot claim effects" in warnings[0].getMessage()


def test_the_dispatcher_rides_out_a_postgresql_restart(restartable_postgres, repo, settings):
    from insurance_claims.store import PostgresStore

    store = PostgresStore(restartable_postgres.url, ttl_seconds=3600, max_sessions=1000, wait_seconds=1,
                          lease_seconds=30, background=False, pool_size=4, pool_timeout=2.0)
    recorder = Recorder()
    d = Dispatcher(store, repo, dataclasses.replace(settings, dispatch_poll_seconds=0.1), email=recorder, handoff=recorder)
    d.start()
    try:
        before = conversation_with(store, email_effect())
        wait_for(lambda: recorder.keys == [delivery_key(before, "email-0")])
        restartable_postgres.stop("fast")
        wait_for(lambda: d.snapshot()["store_errors"] >= 1, timeout=30)
        assert d.running
        restartable_postgres.start()
        after = conversation_with(store, email_effect())  # the store reconnects
        wait_for(lambda: delivery_key(after, "email-0") in recorder.keys, timeout=60)
    finally:
        assert d.stop(timeout=30)
        store.close()
    assert recorder.keys == [delivery_key(before, "email-0"), delivery_key(after, "email-0")]


# ---------------------------------------------------------------------------
# Settings, secrets and the command line
# ---------------------------------------------------------------------------
def _with(settings: Settings, **changes: Any) -> Settings:
    return dataclasses.replace(settings, **changes)


def test_misconfigured_transports_stop_the_start_without_showing_secrets(settings):
    secret_url = "smtps://mailer:pa55word-xyz@mail.harbor.test:465"
    cases = [
        (_with(settings, email_transport="smtp"), "SOP_SMTP_URL"),
        (_with(settings, email_transport="smtp", smtp_url=secret_url), "SOP_EMAIL_FROM"),
        (_with(settings, email_transport="smtp", smtp_url="http://pa55word-xyz@x", email_from="a@b.test"), "SOP_SMTP_URL"),
        (_with(settings, email_transport="carrier-pigeon"), "SOP_EMAIL_TRANSPORT"),
        (_with(settings, handoff_transport="webhook"), "SOP_HANDOFF_WEBHOOK_URL"),
        (_with(settings, handoff_transport="webhook", handoff_webhook_url="ftp://pa55word-xyz@x/"), "SOP_HANDOFF_WEBHOOK_URL"),
        (_with(settings, handoff_transport="webhook", handoff_webhook_url="https://x.test/h",
               handoff_webhook_token="pa55word-xyz\r\nX-Evil: 1"), "SOP_HANDOFF_WEBHOOK_TOKEN"),
        (_with(settings, handoff_transport="pager"), "SOP_HANDOFF_TRANSPORT"),
    ]
    for cfg, setting in cases:
        with pytest.raises(ValueError) as info:
            build_transports(cfg)
        assert setting in str(info.value) and "pa55word" not in str(info.value)

    good = _with(settings, email_transport="smtp", smtp_url=secret_url, email_from="claims@harbor.test",
                 handoff_transport="webhook", handoff_webhook_url="https://hooks.harbor.test/h/pa55word-xyz?sig=1",
                 handoff_webhook_token="pa55word-xyz")
    mail, handoff = build_transports(good)
    d = Dispatcher(SimpleNamespace(kind="memory"), None, good, email=mail, handoff=handoff)  # type: ignore[arg-type]
    shown = repr(mail) + repr(handoff) + repr(good) + repr(d) + json.dumps(d.snapshot())
    assert "pa55word" not in shown and "mail.harbor.test" in shown and "hooks.harbor.test" in shown


def test_the_server_refuses_to_start_with_a_misconfigured_transport(monkeypatch, capsys):
    from insurance_claims import __main__ as cli

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)  # a developer's .env counts not
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setenv("SOP_DISPATCH", "on")
    monkeypatch.setenv("SOP_EMAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("SOP_SMTP_URL", "smtps://mailer:pa55word-xyz@mail.harbor.test")
    monkeypatch.delenv("SOP_EMAIL_FROM", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    err = capsys.readouterr().err
    assert exit_info.value.code == 2 and not calls
    assert "SOP_EMAIL_FROM" in err and "pa55word" not in err

    monkeypatch.setenv("SOP_DISPATCH", "off")  # another process delivers, and checks them at its start
    cli.main([])
    assert len(calls) == 1

    monkeypatch.setenv("SOP_DISPATCH", "on")
    monkeypatch.setenv("SOP_EMAIL_FROM", "claims@harbor.test")
    monkeypatch.setenv("SOP_SHUTDOWN_GRACE_SECONDS", "12")
    cli.main([])
    assert calls[-1]["timeout_graceful_shutdown"] == 12.0


def test_the_dispatcher_command_delivers_what_is_due(monkeypatch, tmp_path, repo, capsys, caplog):
    from insurance_claims.store import SqliteStore

    path = tmp_path / "shared.db"
    store = SqliteStore(path, background=False)
    sid = conversation_with(store, email_effect(), handoff_effect())
    store.close()
    monkeypatch.setenv("SOP_SESSION_STORE", f"sqlite:///{path.as_posix()}")
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    caplog.set_level(logging.INFO, logger="sop.dispatch")
    assert dispatch.main(["--once"]) == 0
    store = SqliteStore(path, background=False)
    try:
        assert outcomes(store, sid) == {"email-0": ("sent", 1, None), "handoff": ("sent", 1, None)}
    finally:
        store.close()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "demo e-mail transport" in logged and "demo handoff transport" in logged

    monkeypatch.setenv("SOP_SESSION_STORE", "memory")  # only the server process could see it
    with pytest.raises(SystemExit) as exit_info:
        dispatch.main(["--once"])
    assert exit_info.value.code == 2 and "SOP_SESSION_STORE" in capsys.readouterr().err


def test_a_transport_bug_does_not_stop_the_round(store_factory, repo, settings):
    class Broken:
        name = "broken"

        def send(self, job: Any) -> None:
            raise KeyError("a bug in a transport")

    store = store_factory()
    recorder = Recorder()
    d = Dispatcher(store, repo, settings, email=Broken(), handoff=recorder)
    sid = conversation_with(store, email_effect(), handoff_effect())
    assert d.run_once() == 2
    assert outcomes(store, sid) == {"email-0": ("pending", 1, "unexpected error (KeyError)"), "handoff": ("sent", 1, None)}
