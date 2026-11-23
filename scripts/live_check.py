"""Several callers at once against a running instance that answers with the live model.

    python scripts/live_check.py http://127.0.0.1:8000              # 12 callers at once
    python scripts/live_check.py http://127.0.0.1:8000 --callers 8

`scripts/load_test.py` refuses the live model on purpose: hundreds of callers would spend real
credit. This is the small version for it. Standard library only.

1. N callers run the demo conversation (five messages, all four phases) at the same moment. The
   wording is the model's, so every conversation is checked on its state: verified, claim CL-2048,
   phases in order, one email sent, turn counter 5.
2. Four messages are sent at the same instant to ONE verified conversation. They must be answered
   one at a time, none lost; while a turn is running the others get `409 conversation_busy` and
   are retried, as the UI does.
3. The message cap, when the instance was started with a small one (for example
   `SOP_MAX_TURNS_PER_CONVERSATION=7`): the cap-th message is answered by a handoff to a human and
   the next one is refused with `409 conversation_closed`. Skipped with the default cap of 60.

With the defaults this is about 65 live turns: roughly 0.2 million input and 0.05 million output
tokens with the default models. It also runs against an offline instance (`--mock`), where it
costs nothing. Prints no message text. Exits non-zero if anything went wrong.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

DEMO = ("I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare "
        "claim from January. DOB is 1985-03-15, SSN last four is 4472.")
SCRIPT = [
    DEMO,
    "How do I submit the missing documents, and how long will the review take after that?",
    "What if I can't get the original pathology report?",
    "That's all I needed, thanks.",
    "Yes, please send it.",
]
BURST = [
    "How do I submit the missing documents?",
    "When is the appeal deadline?",
    "Why was the claim denied?",
    "How long will the review take once you have the documents?",
]
RETRYABLE = {"conversation_busy", "server_busy", "rate_limited"}
MAX_ATTEMPTS = 12
CAP_CHECK_UP_TO = 12  # a larger cap would spend that many live turns on one conversation

base = ""
lock = threading.Lock()
latencies: list[float] = []
refusals: Counter = Counter()
problems: list[str] = []


def call(path: str, body: dict | None = None) -> tuple[int, dict, float]:
    """One request. Returns (status, JSON body, Retry-After in seconds)."""
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read()), 0.0
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": {"code": f"http_{e.code}", "message": raw[:200].decode("utf-8", "replace")}}
        return e.code, payload, float(e.headers.get("Retry-After") or 1.0)


def problem(who: str, what: str) -> None:
    with lock:
        problems.append(f"{who}: {what}")


def expect(who: str, cond: bool, what: str) -> None:
    if not cond:
        problem(who, what)


def say(sid: str, text: str, who: str) -> dict | None:
    """One message, retried on a busy answer the way a client should. None if it was not answered."""
    for _ in range(MAX_ATTEMPTS):
        started = time.perf_counter()
        status, payload, retry_after = call(f"/api/sessions/{sid}/messages", {"text": text})
        if status == 200:
            with lock:
                latencies.append(time.perf_counter() - started)
            return payload
        error = payload.get("error") or {}
        code = error.get("code", f"http_{status}")
        with lock:
            refusals[f"{status} {code}"] += 1
        if code not in RETRYABLE:
            problem(who, f"{status} {code}: {str(error.get('message', ''))[:160]}")
            return None
        time.sleep(min(retry_after, 10.0))
    problem(who, f"still refused after {MAX_ATTEMPTS} attempts")
    return None


def start(who: str) -> str | None:
    status, payload, _ = call("/api/sessions", {"consent_scenario": "default"})
    if status != 200:
        problem(who, f"could not start a conversation ({status})")
        return None
    return payload["session_id"]


def demo_conversation(i: int) -> None:
    who = f"caller {i + 1:02d}"
    sid = start(who)
    if sid is None:
        return
    r = say(sid, SCRIPT[0], who)
    if r is None:
        return
    st = r["state"]
    expect(who, st["verification"]["verified"], "not verified after the demo message")
    expect(who, st["case"]["selected_case_id"] == "CL-2048", f"claim is {st['case']['selected_case_id']}")
    expect(who, st["phase"] == "PROCESS_CASE", f"phase after message 1 is {st['phase']}")
    expect(who, "CL-2048" in r["reply"], "the first reply does not name the claim")
    for k in (1, 2):
        r = say(sid, SCRIPT[k], who)
        if r is None:
            return
        expect(who, r["state"]["phase"] == "PROCESS_CASE", f"phase after message {k + 1} is {r['state']['phase']}")
    r = say(sid, SCRIPT[3], who)
    if r is None:
        return
    expect(who, r["state"]["phase"] == "POST_PROCESS", f"phase after the wrap-up is {r['state']['phase']}")
    expect(who, r["state"]["email"]["offered"], "the email summary was not offered")
    r = say(sid, SCRIPT[4], who)
    if r is None:
        return
    st = r["state"]
    outbox = st["email"]["outbox"]
    expect(who, len(outbox) == 1, f"{len(outbox)} emails in the outbox")
    expect(who, st["status"] == "completed", f"status is {st['status']}")
    expect(who, st["turn"] == len(SCRIPT), f"turn counter is {st['turn']}")
    order = [h["to"] for h in st["phase_history"]]
    expect(who, order == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"], " > ".join(order))
    if outbox:
        body = outbox[0]["body"]
        expect(who, "CL-2048" in body, "the email does not name the claim")
        expect(who, "4472" not in body and "1985" not in body, "the email repeats identity data")


def many_callers(n: int) -> None:
    print(f"1. {n} callers run the demo conversation at the same moment ({n * len(SCRIPT)} turns)")
    before = len(problems)
    started = time.perf_counter()
    threads = [threading.Thread(target=demo_conversation, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started
    seen = sorted(latencies)
    print(f"   {len(seen)} turns answered in {wall:.1f} s; one after another they would take {sum(seen):.0f} s")
    if seen:
        print(f"   turn time seen by the caller: median {statistics.median(seen):.1f} s, "
              f"p95 {seen[max(0, int(len(seen) * 0.95) - 1)]:.1f} s, slowest {seen[-1]:.1f} s")
    wrong = {p.split(":")[0] for p in problems[before:]}
    print(f"   conversations correct: {n - len(wrong)} of {n}")


def burst_on_one_conversation() -> None:
    print(f"2. {len(BURST)} messages to one conversation at the same instant")
    before = len(problems)
    sid = start("burst")
    if sid is None or say(sid, DEMO, "burst setup") is None:
        return
    refused_before = dict(refusals)
    turns: list[int] = []

    def send(k: int) -> None:
        resp = say(sid, BURST[k], f"burst message {k + 1}")
        if resp is not None:
            with lock:
                turns.append(resp["state"]["turn"])

    started = time.perf_counter()
    threads = [threading.Thread(target=send, args=(k,)) for k in range(len(BURST))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started
    _, final, _ = call(f"/api/sessions/{sid}")
    state = final["state"]
    kept = [m for m in final["messages"] if m["role"] != "assistant"]
    told_to_retry = {k: v - refused_before.get(k, 0) for k, v in refusals.items() if v - refused_before.get(k, 0)}
    total = 1 + len(BURST)
    print(f"   {len(turns)} of {len(BURST)} answered in {wall:.1f} s, as turns {sorted(turns)}")
    print(f"   told to retry meanwhile: {told_to_retry or 'none'}")
    expect("burst", sorted(turns) == list(range(2, total + 1)), f"turn numbers {sorted(turns)}")
    expect("burst", state["turn"] == total, f"final turn counter {state['turn']}, expected {total}")
    expect("burst", len(kept) == total, f"{len(kept)} caller messages kept, expected {total}")
    print(f"   {'correct: none lost, none answered twice' if len(problems) == before else 'WRONG'}")


def message_cap(cap: int) -> None:
    if cap > CAP_CHECK_UP_TO:
        print(f"3. message cap: skipped (the cap is {cap}; start the instance with "
              f"SOP_MAX_TURNS_PER_CONVERSATION=7 to include it)")
        return
    print(f"3. message cap of {cap}: message {cap} is handed off, message {cap + 1} is refused")
    before = len(problems)
    sid = start("cap")
    if sid is None:
        return
    texts = [DEMO] + [BURST[k % len(BURST)] for k in range(cap)]
    for n in range(1, cap + 1):
        r = say(sid, texts[n - 1], f"cap message {n}")
        if r is None:
            return
        state = r["state"]
        if n < cap:
            expect("cap", state["status"] == "active", f"message {n} left the conversation {state['status']}")
        else:
            reason = (state["escalation"] or {}).get("reason")
            expect("cap", state["status"] == "escalated" and reason == "turn_limit",
                   f"message {n}: status {state['status']}, handoff reason {reason}")
    status, payload, _ = call(f"/api/sessions/{sid}/messages", {"text": texts[cap]})
    code = (payload.get("error") or {}).get("code")
    expect("cap", status == 409 and code == "conversation_closed", f"message {cap + 1} got {status} {code}")
    _, final, _ = call(f"/api/sessions/{sid}")
    kept = [m for m in final["messages"] if m["role"] != "assistant"]
    expect("cap", len(kept) == cap and final["state"]["turn"] == cap,
           f"{len(kept)} caller messages kept, turn counter {final['state']['turn']}")
    print(f"   {'correct: the refused message changed nothing' if len(problems) == before else 'WRONG'}")


def main() -> int:
    global base
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", help="base URL of a running instance, e.g. http://127.0.0.1:8000")
    parser.add_argument("--callers", type=int, default=12, help="conversations at once in part 1 (default 12)")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    cfg = call("/api/config")[1]
    live = cfg["llm_mode"] == "anthropic" or (cfg["llm_mode"] == "auto" and cfg["has_server_key"])
    print(f"instance {base}: mode {cfg['llm_mode']}, "
          + (f"live model ({cfg['models']['respond']} / {cfg['models']['understand']})" if live else "offline double"))
    many_callers(args.callers)
    burst_on_one_conversation()
    message_cap(cfg["limits"]["max_turns_per_conversation"])

    stats = call("/api/stats")[1]
    counters, lat = stats["counters"], stats["turn_latency_ms"]
    print(f"server side (/api/stats of process {stats['process']['pid']}; each worker counts its own):")
    print(f"   turns answered {counters['turns_answered']}, model errors {counters['model_errors']}, "
          f"503 {counters['refused_busy_503']}, 409 {counters['conflicts_409']}, 429 {counters['rate_limited_429']}")
    print(f"   turn time: median {lat['median_ms']} ms, p95 {lat['p95_ms']} ms over {lat['turns']} turns")
    print(f"refusals seen by the callers, all retried: {dict(refusals) or 'none'}")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print("  " + p)
        return 1
    print("\nALL CORRECT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
