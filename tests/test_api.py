"""HTTP contract (mock model mode)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from insurance_claims import server
from insurance_claims.llm import LLMError

DEMO = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)


@pytest.fixture()
def client():
    return TestClient(server.app)


def test_config_and_fixtures(client):
    cfg = client.get("/api/config").json()
    assert cfg["llm_mode"] == "mock" and cfg["demo_today"] == "2026-03-02"
    assert [p["id"] for p in cfg["phases"]] == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"]
    assert [p["freedom"] for p in cfg["phases"]] == ["strict", "guided", "open", "guided"]
    fixtures = client.get("/api/fixtures").json()
    assert {p["party_id"] for p in fixtures["policyholders"]} == {"P9", "P7", "P12", "P13"}


def test_conversation_over_http(client):
    created = client.post("/api/sessions", json={"consent_scenario": "default"}).json()
    sid = created["session_id"]
    assert created["messages"][0]["role"] == "assistant"
    assert created["state"]["phase"] == "VERIFY_ID" and not created["state"]["verification"]["verified"]

    out = client.post(f"/api/sessions/{sid}/messages", json={"text": DEMO}).json()
    state = out["state"]
    assert state["phase"] == "PROCESS_CASE" and state["verification"]["verified"]
    assert state["verification"]["matched_count"] == 3
    assert state["verification"]["fields"]["dob"]["status"] == "matched"
    assert state["verification"]["policy_number"]["status"] == "matched"
    assert state["case"]["selected_case_id"] == "CL-2048"
    assert {h["key"] for h in state["memory"]["claim_hints"]} >= {"case_type", "status", "time_reference"}
    assert all(h["captured_phase"] == "VERIFY_ID" for h in state["memory"]["claim_hints"])
    assert out["trace"]["phase_before"] == "VERIFY_ID" and out["trace"]["phase_after"] == "PROCESS_CASE"
    kinds = {e["kind"] for e in out["trace"]["events"]}
    assert {"understand", "memory", "tool", "gate", "transition", "decision", "directive", "guard"} <= kinds
    # The state view and trace never carry the identifier values themselves.
    blob = str(state) + str(out["trace"])
    assert "4472" not in blob and "1985-03-15" not in blob

    full = client.get(f"/api/sessions/{sid}").json()
    assert len(full["messages"]) == 3 and len(full["traces"]) == 1


def test_errors_use_the_error_envelope(client):
    r = client.post("/api/sessions/nope/messages", json={"text": "hi"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "session_not_found"
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_failed_model_call_leaves_the_conversation_untouched(client, monkeypatch):
    sid = client.post("/api/sessions", json={}).json()["session_id"]

    class Broken:
        name = "broken"

        def understand(self, *a, **k):
            raise LLMError("invalid_api_key", "The model API key was rejected.", 401)

    monkeypatch.setattr(server, "get_backend", lambda key: Broken())
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": DEMO})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    after = client.get(f"/api/sessions/{sid}").json()
    assert after["state"]["turn"] == 0 and len(after["messages"]) == 1  # nothing was recorded

    monkeypatch.undo()
    ok = client.post(f"/api/sessions/{sid}/messages", json={"text": DEMO})
    assert ok.status_code == 200 and ok.json()["state"]["turn"] == 1  # retry works cleanly


def test_missing_key_is_reported_in_model_mode(monkeypatch):
    from insurance_claims.config import load_settings
    from insurance_claims.llm import AnthropicBackend

    settings = load_settings()  # (this may load a developer's .env; the key is removed below)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(LLMError) as err:
        AnthropicBackend(settings)
    assert err.value.code == "missing_api_key" and err.value.status == 401


def test_auto_mode_answers_offline_until_a_key_is_available(client, monkeypatch):
    """SOP_LLM_MODE=auto, as on the hosted demo: no key -> the offline double; the tester's key
    -> the model with that key; a server key -> the model for everyone."""
    import dataclasses

    from insurance_claims.mock_llm import MockBackend

    built: list[str | None] = []

    class FakeModel(MockBackend):  # stands in for the Claude backend; records the key it was given
        def __init__(self, settings, key=None):
            super().__init__()
            built.append(key)

    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, llm_mode="auto"))
    monkeypatch.setattr(server, "AnthropicBackend", FakeModel)
    monkeypatch.setattr(server, "_backends", {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    cfg = client.get("/api/config").json()
    assert cfg["llm_mode"] == "auto" and cfg["has_server_key"] is False
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    url = f"/api/sessions/{sid}/messages"

    out = client.post(url, json={"text": "My name is Margaret Chen."})
    assert out.status_code == 200 and built == []  # no key anywhere: nothing but the double
    assert isinstance(server.get_backend(None), MockBackend) and built == []

    out = client.post(url, json={"text": "My date of birth is 1985-03-15."},
                      headers={"X-API-Key": "test-key-from-the-tester"})
    assert out.status_code == 200 and built == ["test-key-from-the-tester"]
    client.post(url, json={"text": "What is RL?"})  # the next request without a key is offline again
    assert built == ["test-key-from-the-tester"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-on-the-server")
    assert client.get("/api/config").json()["has_server_key"] is True
    out = client.post(url, json={"text": "My phone is 650-521-2836."})
    assert out.status_code == 200 and built == ["test-key-from-the-tester", None]  # the server's key
    assert out.json()["state"]["verification"]["verified"]  # one conversation across both backends


def test_mock_mode_ignores_a_key_and_model_mode_is_unchanged(client, monkeypatch):
    import dataclasses

    from insurance_claims.mock_llm import MockBackend

    monkeypatch.setattr(server, "_backends", {})
    assert isinstance(server.get_backend("some-key"), MockBackend)  # tests run in mock mode

    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, llm_mode="anthropic"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    out = client.post(f"/api/sessions/{sid}/messages", json={"text": "hello"})
    assert out.status_code == 401 and out.json()["error"]["code"] == "missing_api_key"


def test_the_mode_setting_accepts_auto_and_falls_back_to_the_model(monkeypatch):
    from insurance_claims.config import load_settings

    for raw, expected in (("auto", "auto"), ("AUTO", "auto"), ("mock", "mock"), ("nonsense", "anthropic")):
        monkeypatch.setenv("SOP_LLM_MODE", raw)
        assert load_settings().llm_mode == expected


def test_concurrent_messages_to_one_conversation_are_serialised(client, monkeypatch):
    """Two requests in flight for the same conversation must not overwrite each other."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    real_turn = server.agent.handle_turn

    def slow_turn(state, text, backend):
        time.sleep(0.15)  # hold the turn open so the other requests are genuinely waiting
        return real_turn(state, text, backend)

    monkeypatch.setattr(server.agent, "handle_turn", slow_turn)
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    texts = ["My name is Margaret Chen.", "My date of birth is 1985-03-15.", "My phone is 650-521-2836."]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(
            lambda t: client.post(f"/api/sessions/{sid}/messages", json={"text": t}).status_code, texts))
    assert results == [200, 200, 200]
    after = client.get(f"/api/sessions/{sid}").json()
    assert after["state"]["turn"] == 3 and len(after["messages"]) == 7  # no turn was lost
    assert after["state"]["verification"]["verified"]  # all three identifiers were counted
