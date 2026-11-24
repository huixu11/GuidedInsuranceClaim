"""Encryption at rest (SOP_DATA_KEY) in the SQL stores: AES-256-GCM per value, each value bound to
its table, conversation and row. State, messages, traces and effect payloads are encrypted; the
audit trail is not (it holds no personal data, and is read after the conversation is gone)."""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import uuid
from pathlib import Path

import pytest

from insurance_claims.config import parse_data_key
from insurance_claims.state import SessionState
from insurance_claims.store import EncryptedDataError, open_store
from insurance_claims.store.crypto import PREFIX, Cipher

SQL = pytest.mark.parametrize("store_kind", ["sqlite", "postgres"], indirect=True)
KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
MARKER = "ZEBRA-7731-SECRET"


def secret_state() -> SessionState:
    s = SessionState(session_id=uuid.uuid4().hex)
    s.identity_claims = {"full_name": f"Margaret {MARKER}", "dob": "1985-03-15"}
    s.messages.append({"role": "user", "text": f"my name is {MARKER}", "turn": 0})
    s.traces.append({"turn": 0, "note": f"trace {MARKER}"})
    s.audit("conversation_started", consent_scenario="default", phase="VERIFY_ID")
    return s


def rows(store, sql: str) -> list:
    return [tuple(row) for row in store._session(lambda ex: ex.run(sql).rows)]


def stored_text(store) -> dict[str, list[str]]:
    return {
        "conversations": [r[0] for r in rows(store, "SELECT state FROM conversations")],
        "messages": [r[0] for r in rows(store, "SELECT data FROM messages ORDER BY session_id, seq")],
        "traces": [r[0] for r in rows(store, "SELECT data FROM traces ORDER BY session_id, seq")],
        "outbox": [r[0] for r in rows(store, "SELECT payload FROM outbox ORDER BY id")],
        "audit_events": [r[0] for r in rows(store, "SELECT data FROM audit_events ORDER BY session_id, seq")],
    }


# ---------------------------------------------------------------------------
# The key and the cipher
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    base64.b64encode(KEY).decode(),
    base64.b64encode(KEY).decode().rstrip("="),
    base64.urlsafe_b64encode(bytes(range(200, 232))).decode(),
    KEY.hex(),
    KEY.hex().upper(),
    f"  {KEY.hex()}\n",
])
def test_a_data_key_is_base64_or_hex_of_32_bytes(text):
    assert len(parse_data_key(text)) == 32


@pytest.mark.parametrize("text", [
    "correct horse battery staple",
    base64.b64encode(bytes(31)).decode(),
    base64.b64encode(bytes(33)).decode(),
    KEY.hex()[:-2],
    "Z" * 44 + "!",
])
def test_anything_else_is_refused_without_repeating_it(text):
    with pytest.raises(ValueError) as refused:
        parse_data_key(text)
    assert text.strip() not in str(refused.value) and "SOP_DATA_KEY" in str(refused.value)
    assert parse_data_key("") is None and parse_data_key(None) is None


def test_a_malformed_key_stops_the_start(monkeypatch):
    from insurance_claims.config import load_settings

    monkeypatch.setenv("SOP_DATA_KEY", "my-secret-passphrase")
    with pytest.raises(ValueError) as refused:
        load_settings()
    assert "my-secret-passphrase" not in str(refused.value) and "os.urandom(32)" in str(refused.value)


def test_each_value_opens_only_in_its_own_place_and_with_its_own_key():
    cipher = Cipher(KEY)
    sealed = cipher.seal('{"text": "hello"}', "messages", "conv-1", 3)
    assert sealed.startswith(PREFIX) and "hello" not in sealed
    assert sealed != cipher.seal('{"text": "hello"}', "messages", "conv-1", 3)  # a fresh nonce each time
    assert cipher.open(sealed, "messages", "conv-1", 3) == '{"text": "hello"}'
    for elsewhere in (("traces", "conv-1", 3), ("messages", "conv-2", 3), ("messages", "conv-1", 4)):
        with pytest.raises(EncryptedDataError, match="could not be decrypted"):
            cipher.open(sealed, *elsewhere)  # a value copied to another row does not open
    tampered = sealed[:-6] + ("A" if sealed[-6] != "A" else "B") + sealed[-5:]
    with pytest.raises(EncryptedDataError):
        cipher.open(tampered, "messages", "conv-1", 3)
    with pytest.raises(EncryptedDataError, match="could not be decrypted"):
        Cipher(OTHER_KEY).open(sealed, "messages", "conv-1", 3)
    with pytest.raises(EncryptedDataError, match="no data key"):
        Cipher(None).open(sealed, "messages", "conv-1", 3)
    assert cipher.open('{"plain": true}', "messages", "conv-1", 3) == '{"plain": true}'
    assert Cipher(None).seal("as is", "messages", "conv-1") == "as is"
    with pytest.raises(ValueError):
        Cipher(bytes(16))


# ---------------------------------------------------------------------------
# In the stores
# ---------------------------------------------------------------------------
@SQL
def test_nothing_personal_is_readable_at_rest(store_factory, store_kind, tmp_path):
    store = store_factory(data_key=KEY)
    s = secret_state()
    store.create(s)
    with store.turn(s.session_id) as t:
        t.state.messages.append({"role": "assistant", "text": f"thanks, {MARKER}", "turn": 1})
        t.state.traces.append({"turn": 1, "note": MARKER})
        t.state.audit("email_queued", recipient="caller", case_ids=["CL-1"], summary_source="template")
        t.commit(effects=[{"kind": "email", "key": "summary", "payload": {"body": f"Dear {MARKER}"}}])
    expected = store.read(s.session_id)
    assert expected.messages[-1]["text"] == f"thanks, {MARKER}" and expected.identity_claims == s.identity_claims
    assert store.stats()["encrypted_at_rest"] is True

    stored = stored_text(store)
    for table in ("conversations", "messages", "traces", "outbox"):
        assert stored[table] and all(value.startswith(PREFIX) for value in stored[table]), table
    assert [json.loads(v)["recipient"] for v in stored["audit_events"][1:]] == ["caller"]  # readable, by design
    assert MARKER not in json.dumps(stored)
    if store_kind == "sqlite":
        files = [p for p in Path(tmp_path).glob("sessions.db*") if p.is_file()]
        assert files and not [p.name for p in files if MARKER.encode() in p.read_bytes()]

    (claim,) = store.claim_effects()
    assert claim["payload"] == {"body": f"Dear {MARKER}"}
    reopened = store_factory(data_key=KEY)
    assert reopened.read(s.session_id) == expected


@SQL
def test_a_wrong_or_missing_key_is_a_clear_error(store_factory):
    writer = store_factory(data_key=KEY)
    s = secret_state()
    writer.create(s)
    with writer.turn(s.session_id) as t:
        t.commit(effects=[{"kind": "email", "key": "summary", "payload": {"body": MARKER}}])

    wrong = store_factory(data_key=OTHER_KEY, clock=writer.test_clock)
    with pytest.raises(EncryptedDataError, match="could not be decrypted"):
        wrong.read(s.session_id)
    with pytest.raises(EncryptedDataError):
        with wrong.turn(s.session_id):
            pass
    with writer.turn(s.session_id, wait=0) as t:  # the failed turn let go of the conversation
        t.commit()

    keyless = store_factory(clock=writer.test_clock)
    with pytest.raises(EncryptedDataError, match="no data key is configured"):
        keyless.read(s.session_id)
    # What is not encrypted stays readable without the key.
    assert [e["type"] for e in keyless.audit(s.session_id)] == ["conversation_started"]
    assert [e["key"] for e in keyless.effects(s.session_id)] == ["summary"]
    # An effect whose payload cannot be opened is set aside, not handed out again and again.
    assert wrong.claim_effects() == []
    (dead,) = writer.effects(s.session_id)
    assert dead["status"] == "dead" and dead["last_error"].startswith("payload cannot be read")


@SQL
def test_rows_written_before_the_key_was_set_stay_readable(store_factory):
    plain = store_factory()
    s = secret_state()
    plain.create(s)
    with plain.turn(s.session_id) as t:
        t.state.messages.append({"role": "assistant", "text": "before the key", "turn": 1})
        t.commit()

    keyed = store_factory(data_key=KEY, clock=plain.test_clock)
    before = keyed.read(s.session_id)
    assert before.messages[-1]["text"] == "before the key"
    with keyed.turn(s.session_id) as t:
        t.state.messages.append({"role": "assistant", "text": "after the key", "turn": 2})
        t.commit()
    messages = stored_text(keyed)["messages"]
    assert [m.startswith(PREFIX) for m in messages] == [False, False, True]
    assert stored_text(keyed)["conversations"][0].startswith(PREFIX)  # rewritten by the commit
    assert [m["text"] for m in keyed.read(s.session_id).messages] == [
        f"my name is {MARKER}", "before the key", "after the key"
    ]
    with pytest.raises(EncryptedDataError):  # and from now on the key is needed
        plain.read(s.session_id)


def test_a_shared_store_without_a_key_warns_once_at_start(tmp_path, caplog):
    from insurance_claims.config import load_settings

    cfg = load_settings()
    with caplog.at_level(logging.WARNING, logger="sop.store"):
        store = open_store(dataclasses.replace(cfg, session_store=str(tmp_path / "plain.db")))
        store.close()
    assert [r.getMessage() for r in caplog.records if "unencrypted" in r.getMessage()] == [
        f"{store.describe()} keeps conversations unencrypted at rest; set SOP_DATA_KEY to encrypt them"
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sop.store"):
        store = open_store(
            dataclasses.replace(cfg, session_store=str(tmp_path / "keyed.db"), data_key=base64.b64encode(KEY).decode())
        )
        store.close()
    assert store.encrypted and not [r for r in caplog.records if "unencrypted" in r.getMessage()]
