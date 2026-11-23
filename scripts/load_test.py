"""Many callers at once, against one or more running instances, with every conversation checked.

    python scripts/load_test.py http://127.0.0.1:8000 --callers 500 --concurrency 100
    python scripts/load_test.py http://127.0.0.1:8001 http://127.0.0.1:8002 --callers 500   # two instances, one store

Standard library only. Run it against the offline double (`SOP_LLM_MODE=mock`): it costs nothing and
measures the harness itself. Each simulated caller holds a whole conversation and the script checks
it, not just the status codes:

- every turn is answered and the turn counter moves by exactly one (no lost or doubled updates);
- the conversation ends where its script says it must (verified and on the right claim, e-mail
  sent or skipped, handed to a human), so one caller's state never bleeds into another's;
- no reply mentions a claim number that belongs to a different policyholder.

With several base URLs each request goes to the next one in turn, so a conversation is served by
all of them. That only works when the instances share a session store.

Exit status is non-zero if any conversation went wrong.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

CLAIM_ID = re.compile(r"\bCL-\d+\b")
DEMO = ("I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
        "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.")

_local = threading.local()


class TurnFailed(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"HTTP {status} {code}")
        self.status, self.code = status, code


def _conn(base: str) -> http.client.HTTPConnection:
    """One keep-alive connection per worker thread and instance."""
    pool = _local.__dict__.setdefault("pool", {})
    conn = pool.get(base)
    if conn is None:
        u = urlparse(base)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = pool[base] = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=120)
    return conn


def call(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict, dict]:
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    for attempt in (1, 2):
        conn = _conn(base)
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {"error": {"code": "not_json", "message": raw[:80].decode("utf-8", "replace")}}
            return resp.status, data, dict(resp.getheaders())
        except (http.client.HTTPException, OSError):
            conn.close()
            _local.pool.pop(base, None)
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


class Caller:
    """One conversation. `bases` are rotated per request."""

    def __init__(self, index: int, bases: list[str], latencies: list[float], retries: Counter) -> None:
        self.i, self.bases, self.lat, self.retries = index, bases, latencies, retries
        self.n = index  # request counter, offset so callers start on different instances
        self.sid = ""
        self.state: dict = {}
        self.replies: list[str] = []

    def _base(self) -> str:
        self.n += 1
        return self.bases[self.n % len(self.bases)]

    def _request(self, method: str, path: str, body: dict | None, timed: bool) -> dict:
        """Honour 'busy, come back later' answers (429/503/409) a few times, as a client would."""
        for attempt in range(40):
            t = time.perf_counter()
            status, data, headers = call(self._base(), method, path, body)
            if status == 200:
                if timed:
                    self.lat.append(time.perf_counter() - t)
                return data
            code = str((data.get("error") or {}).get("code", ""))
            if status in (409, 429, 503) and attempt < 39:
                self.retries[f"{status} {code}"] += 1
                try:
                    wait = float(headers.get("Retry-After") or headers.get("retry-after") or 0.2)
                except ValueError:
                    wait = 0.2
                time.sleep(min(max(wait, 0.05), 2.0))
                continue
            raise TurnFailed(status, code)
        raise AssertionError("unreachable")

    def start(self, scenario: str = "default") -> None:
        data = self._request("POST", "/api/sessions", {"consent_scenario": scenario}, timed=False)
        self.sid, self.state = data["session_id"], data["state"]

    def say(self, text: str) -> None:
        before = self.state.get("turn", 0)
        data = self._request("POST", f"/api/sessions/{self.sid}/messages", {"text": text}, timed=True)
        self.state = data["state"]
        self.replies.append(data["reply"])
        if self.state["turn"] != before + 1:
            raise AssertionError(f"turn counter went {before} -> {self.state['turn']}")

    def reread(self) -> None:
        """What a reload would see must be what the last turn returned."""
        data = self._request("GET", f"/api/sessions/{self.sid}", None, timed=False)
        if data["state"] != self.state:
            raise AssertionError("state read back differs from the state the last turn returned")
        if len([m for m in data["messages"] if m["role"] == "user"]) != self.state["turn"]:
            raise AssertionError("transcript length does not match the turn counter")


def expect(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)


# ---- scripted conversations (worded for the offline double) --------------------------------------

def s_demo_send(c: Caller, fx: dict) -> None:
    c.start()
    c.say(DEMO)
    expect(c.state["verification"]["verified"] and c.state["case"]["selected_case_id"] == "CL-2048", "demo: not on CL-2048")
    c.say("How do I submit the missing documents?")
    c.say("That's all I needed, thanks.")
    expect(c.state["phase"] == "POST_PROCESS" and c.state["email"]["offered"], "demo: no e-mail offer")
    c.say("Yes, please send it.")
    expect(c.state["status"] == "completed" and len(c.state["email"]["outbox"]) == 1, "demo: e-mail not sent once")
    c.reread()


def s_demo_skip(c: Caller, fx: dict) -> None:
    c.start()
    c.say(DEMO)
    c.say("That's all I needed, thanks.")
    c.say("No thanks, skip the email.")
    expect(c.state["status"] == "completed" and not c.state["email"]["outbox"]
           and c.state["email"]["decision"] == "skip", "skip: an e-mail was sent or the call did not complete")
    c.reread()


def s_memory(c: Caller, fx: dict) -> None:
    c.start()
    c.say("I'm calling about my denied healthcare claim from January.")
    expect(c.state["phase"] == "VERIFY_ID" and not CLAIM_ID.search(c.replies[-1]), "memory: left VERIFY_ID or leaked")
    c.say("My name is Margaret Chen and my date of birth is 1985-03-15.")
    expect(not c.state["verification"]["verified"] and c.state["verification"]["matched_count"] == 2, "memory: 2 fields")
    c.say("My phone number is 650-521-2836.")
    expect(c.state["verification"]["verified"] and c.state["case"]["selected_case_id"] == "CL-2048", "memory: hint unused")
    c.reread()


def s_refuse(c: Caller, fx: dict) -> None:
    c.start()
    for text in ("Just tell me why my claim was denied. I'm not going to verify anything.",
                 "I already told you who I am, just tell me.", "This is ridiculous, I won't verify, just tell me!"):
        c.say(text)
        expect(not CLAIM_ID.search(c.replies[-1]), "refuse: a claim number was disclosed")
    expect(c.state["status"] == "escalated" and not c.state["verification"]["verified"]
           and c.state["case"]["selected_case_id"] is None, "refuse: not handed off, or something was unlocked")
    c.reread()


def s_off_topic(c: Caller, fx: dict) -> None:
    c.start()
    for text in ("What is RL?", "Come on, just explain reinforcement learning.", "What's the weather today?", "Tell me a joke."):
        c.say(text)
    expect(c.state["status"] == "escalated" and c.state["phase"] == "VERIFY_ID", "off-topic: not handed off")
    c.reread()


def s_mismatch(c: Caller, fx: dict) -> None:
    c.start()
    for text in ("I'm Margaret Chen, date of birth 1985-03-16.", "Sorry, SSN last four is 1111.", "My phone is 555-000-1234."):
        c.say(text)
    expect(c.state["status"] == "escalated" and not c.state["verification"]["verified"], "mismatch: not locked out")
    c.reread()


def s_each_policyholder(c: Caller, fx: dict) -> None:
    """A different policyholder per caller: their own claims and nobody else's."""
    people = fx.get("policyholders") or []
    if not people:
        return s_memory(c, fx)
    p = people[c.i % len(people)]
    own = {cl["case_id"] for cl in p["claims"]}
    c.start()
    c.say(f"My name is {p['name']}. My date of birth is {p['dob']}. The last four of my ID are {p['id_last4']}.")
    expect(c.state["verification"]["verified"], f"{p['party_id']}: three matching identifiers did not verify")
    expect(c.state["case"]["claims_on_file"] == len(own), f"{p['party_id']}: wrong number of claims on file")
    c.say("What is the status of my claim?")
    seen = set(CLAIM_ID.findall(" ".join(c.replies)))
    expect(seen <= own, f"{p['party_id']}: reply mentions someone else's claim {sorted(seen - own)}")
    selected = c.state["case"]["selected_case_id"]
    expect(selected is None or selected in own, f"{p['party_id']}: selected a claim that is not theirs")
    c.reread()


SCENARIOS = [s_demo_send, s_each_policyholder, s_memory, s_demo_skip, s_refuse, s_each_policyholder, s_off_topic, s_mismatch]


def burst_on_one_conversation(bases: list[str], n: int) -> str:
    """n messages sent at the same instant to one conversation: each is answered once, in some order."""
    lat: list[float] = []
    c = Caller(0, bases, lat, Counter())
    c.start()
    results: list[int] = []

    def send(k: int) -> None:
        status = 0
        for _ in range(60):
            status, _, headers = call(bases[k % len(bases)], "POST", f"/api/sessions/{c.sid}/messages", {"text": "What is RL?"})
            if status not in (409, 429, 503):
                break
            time.sleep(0.1)
        results.append(status)

    threads = [threading.Thread(target=send, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _, data, _ = call(bases[0], "GET", f"/api/sessions/{c.sid}")
    ok = sum(1 for s in results if s == 200)
    turn = data["state"]["turn"]
    users = len([m for m in data["messages"] if m["role"] == "user"])
    good = ok == n and turn == n and users == n
    return f"{'ok  ' if good else 'FAIL'} burst of {n} on one conversation: {ok} answered, turn counter {turn}, {users} caller messages stored"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bases", nargs="+", help="base URL(s) of the running instance(s)")
    ap.add_argument("--callers", type=int, default=200, help="conversations to hold in total")
    ap.add_argument("--concurrency", type=int, default=50, help="conversations in progress at once")
    ap.add_argument("--burst", type=int, default=8, help="simultaneous messages to one conversation (0 = skip)")
    args = ap.parse_args()
    bases = [b.rstrip("/") for b in args.bases]

    status, cfg, _ = call(bases[0], "GET", "/api/config")
    if status != 200:
        print("cannot read /api/config:", status)
        return 2
    print(f"instances: {len(bases)} | llm_mode: {cfg.get('llm_mode')} | callers: {args.callers} | at once: {args.concurrency}")
    if cfg.get("llm_mode") == "anthropic" or (cfg.get("llm_mode") == "auto" and cfg.get("has_server_key")):
        print("refusing to load-test the live model; start the instance with SOP_LLM_MODE=mock")
        return 2
    status, fixtures, _ = call(bases[0], "GET", "/api/fixtures")
    fixtures = fixtures if status == 200 else {}

    latencies: list[float] = []
    retries: Counter = Counter()
    failures: list[str] = []
    done = Counter()
    lock = threading.Lock()

    def one(i: int) -> None:
        scenario = SCENARIOS[i % len(SCENARIOS)]
        lat: list[float] = []
        c = Caller(i, bases, lat, retries)
        try:
            scenario(c, fixtures)
            outcome = "ok"
        except TurnFailed as exc:
            outcome = f"{scenario.__name__}: turn refused ({exc})"
        except AssertionError as exc:
            outcome = f"{scenario.__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a transport failure is a finding, not a crash
            outcome = f"{scenario.__name__}: {type(exc).__name__}: {exc}"
        with lock:
            latencies.extend(lat)
            done[outcome == "ok"] += 1
            if outcome != "ok" and len(failures) < 12:
                failures.append(f"caller {i}: {outcome}")

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(one, range(args.callers)))
    wall = time.perf_counter() - started

    turns = len(latencies)
    print(f"conversations: {done[True]} correct, {done[False]} wrong | turns answered: {turns} in {wall:.1f}s "
          f"= {turns / wall:.0f} turns/s, {args.callers / wall:.1f} conversations/s")
    if latencies:
        print(f"turn latency: median {statistics.median(latencies) * 1000:.0f} ms | p95 {pct(latencies, 95) * 1000:.0f} ms | "
              f"p99 {pct(latencies, 99) * 1000:.0f} ms | max {max(latencies) * 1000:.0f} ms")
    if retries:
        print("asked to come back later:", dict(retries))
    for line in failures:
        print("  WRONG", line)

    burst_ok = True
    if args.burst:
        line = burst_on_one_conversation(bases, args.burst)
        burst_ok = line.startswith("ok")
        print(line)

    status, stats, _ = call(bases[0], "GET", "/api/stats")
    if status == 200:
        print("server stats:", json.dumps(stats))
    return 0 if not done[False] and burst_ok else 1


if __name__ == "__main__":
    sys.exit(main())
