"""Smoke test for a running container (or any running instance of the app).

    python scripts/docker_smoke.py http://127.0.0.1:8000            # started with SOP_LLM_MODE=mock
    python scripts/docker_smoke.py http://127.0.0.1:8001 --no-key   # model mode, no API key supplied

The first form walks the demo test case through all four phases over HTTP. The second checks
that an instance without a model key says so cleanly instead of failing obscurely.

Standard library only, so it runs on a bare CI runner. Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILURES.append(label)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        status, raw = self.call(method, path, body)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {}


def offline_workflow(c: Client) -> None:
    print("server and static files")
    status, body = c.json("GET", "/healthz")
    check("GET /healthz", status == 200 and body.get("status") == "ok", status)
    status, raw = c.call("GET", "/")
    check("GET / serves the test UI", status == 200 and b"<html" in raw.lower(), f"{status}, {len(raw)} bytes")
    for path in ("/static/app.js", "/static/styles.css"):
        status, raw = c.call("GET", path)
        check(f"GET {path}", status == 200 and len(raw) > 0, f"{status}, {len(raw)} bytes")
    status, cfg = c.json("GET", "/api/config")
    check("config reports offline mode", status == 200 and cfg.get("llm_mode") == "mock", cfg.get("llm_mode"))
    check("config lists the four phases", len(cfg.get("phases", [])) == 4, len(cfg.get("phases", [])))

    print("demo test case, end to end")
    status, session = c.json("POST", "/api/sessions", {"consent_scenario": "default"})
    sid = session.get("session_id", "")
    state = session.get("state", {})
    check("session created", status == 200 and bool(sid), status)
    check(
        "starts in VERIFY_ID, unverified",
        state.get("phase") == "VERIFY_ID" and state.get("verification", {}).get("verified") is False,
        state.get("phase"),
    )

    def turn(text: str) -> tuple[str, dict]:
        status, out = c.json("POST", f"/api/sessions/{sid}/messages", {"text": text})
        st = out.get("state", {})
        reply = out.get("reply", "")
        print(f"    caller: {text}")
        print(f"    agent : {reply[:160]}{'...' if len(reply) > 160 else ''}")
        print(
            f"    -> HTTP {status} | {st.get('phase')} | {st.get('status')} | "
            f"verified={st.get('verification', {}).get('verified')} | "
            f"claim={st.get('case', {}).get('selected_case_id')} | "
            f"emails={len(st.get('email', {}).get('outbox', []))}"
        )
        check("turn answered", status == 200 and bool(reply.strip()), status)
        return reply, st

    reply, st = turn("Why was my claim denied?")
    check(
        "gate holds before verification (no phase change, no claim data)",
        st.get("phase") == "VERIFY_ID"
        and st["verification"]["verified"] is False
        and st["case"]["selected_case_id"] is None
        and "CL-" not in reply,
        st.get("phase"),
    )

    reply, st = turn(
        "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
        "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
    )
    check("identity verified with three identifiers", st["verification"]["verified"] is True
          and st["verification"]["matched_count"] >= 3, st["verification"]["matched_count"])
    check("claim resolved from what was said during verification",
          st["case"]["selected_case_id"] == "CL-2048", st["case"]["selected_case_id"])
    check("moved on to PROCESS_CASE", st.get("phase") == "PROCESS_CASE", st.get("phase"))

    reply, st = turn("How do I submit the missing documents?")
    check("stays in PROCESS_CASE for a follow-up", st.get("phase") == "PROCESS_CASE", st.get("phase"))

    reply, st = turn("That's all I needed, thanks.")
    check("POST_PROCESS offers the summary, nothing sent yet",
          st.get("phase") == "POST_PROCESS" and len(st["email"]["outbox"]) == 0, st.get("phase"))

    reply, st = turn("Yes, please send it.")
    check("summary email sent once", len(st["email"]["outbox"]) == 1, len(st["email"]["outbox"]))
    check("conversation completed", st.get("status") == "completed", st.get("status"))
    check("phase history covers all four phases",
          [p for p in ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")
           if p in json.dumps(st.get("phase_history", []))]
          == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"])

    print("errors")
    status, body = c.json("GET", "/api/sessions/does-not-exist")
    check("unknown session is a clean 404", status == 404
          and body.get("error", {}).get("code") == "session_not_found", status)


def no_key(c: Client) -> None:
    print("model mode without a key")
    status, body = c.json("GET", "/healthz")
    check("GET /healthz", status == 200 and body.get("status") == "ok", status)
    status, cfg = c.json("GET", "/api/config")
    check("config reports model mode", cfg.get("llm_mode") == "anthropic", cfg.get("llm_mode"))
    check("config reports that no server key is set", cfg.get("has_server_key") is False, cfg.get("has_server_key"))
    status, session = c.json("POST", "/api/sessions", {})
    sid = session.get("session_id", "")
    status, body = c.json("POST", f"/api/sessions/{sid}/messages", {"text": "hello"})
    code = body.get("error", {}).get("code")
    check("a message without any key is refused with missing_api_key", status == 401 and code == "missing_api_key",
          f"{status} {code}")
    status, session = c.json("GET", f"/api/sessions/{sid}")
    check("the refused turn left the conversation untouched", session.get("state", {}).get("turn") == 0,
          session.get("state", {}).get("turn"))


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit(__doc__)
    client = Client(args[0])
    (no_key if "--no-key" in sys.argv[1:] else offline_workflow)(client)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for label in FAILURES:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
