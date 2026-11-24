"""Kill an instance while callers are mid-conversation: every conversation must still end right.

    python scripts/failover_test.py                                   # 3 instances, a temporary SQLite file
    python scripts/failover_test.py --store postgresql://user:pw@host/db --callers 80
    python scripts/failover_test.py --no-restart --kill-after 2
    # instances that are already running, e.g. docker-compose.ha.yml behind its load balancer:
    python scripts/failover_test.py --urls http://127.0.0.1:8080 \
        --kill-cmd "docker kill sop-ha-app2-1" --restart-cmd "docker start sop-ha-app2-1"
    # instances on the live model, started by hand (a turn takes 5-15 s there, and costs credit):
    python scripts/failover_test.py --urls http://127.0.0.1:8066,http://127.0.0.1:8067 --callers 8 \
        --kill-after 4 --wait-margin 30 --give-up 240 --kill-cmd "kill -9 <pid of one of them>"

It starts the instances itself (offline double, each model call slowed down so that turns are in
flight when the kill comes), all on one shared session store, and plays the callers the way a
browser behind a load balancer behaves: every message carries its own id, goes to the next
instance that answers, and is sent again with the same id after a broken connection or a
"busy, come back" answer. Part-way through, one instance is killed outright (no shutdown, as in a
crash or a lost machine) and, unless --no-restart, started again a little later.

What must hold, and is checked for every conversation:

- it ends where its script says (verified, on the right claim, all four phases, e-mail sent);
- every message was answered exactly once: the turn counter and the transcript show each of the
  caller's messages once, in order. A message whose answer was lost with the killed instance is
  not processed twice when it is sent again, and none is dropped;
- no caller waited longer for one answer than the turn lease plus a margin: a conversation the
  killed instance was working on is free again once that lease lapses;
- afterwards every instance, the restarted one included, shows the same finished conversations;
- every conversation's summary e-mail was queued once and is sent (the audit endpoint), and was
  delivered once (the demo transport's log lines of every instance). The one exception allowed
  is the at-least-once case: the killed instance delivered it and died before it could record
  that, and another instance delivered it again once the claim lapsed;
- the restarted instance answers /readyz with "ready".

With --urls nothing is started here: the callers go to the given base URLs (one load balancer, or
several instances), --kill-cmd kills one instance and --restart-cmd brings it back. The instance
logs are then out of reach, so "delivered once" is not checked from them; everything else is.

Standard library only. Exit status is non-zero if anything went wrong.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DEMO = ("I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare "
        "claim from January. DOB is 1985-03-15, SSN last four is 4472.")
SCRIPT = [
    DEMO,
    "How do I submit the missing documents, and how long will the review take after that?",
    "What if I can't get the original pathology report?",
    "That's all I needed, thanks.",
    "Yes, please send it.",
]
RETRY_STATUS = {409, 429, 502, 503, 504}  # busy or briefly unavailable: send the same message again
DOWN_FOR = 1.0  # seconds an instance is skipped after a refused or broken connection (a health check)

lock = threading.Lock()
problems: list[str] = []
retries: Counter = Counter()
waits: list[float] = []  # seconds from the first attempt of a message to its answer
answered_at: list[float] = []
replays = 0
inflight: dict[int, tuple[str, str]] = {}  # thread id -> (base, "turn" | "other")
down_until: dict[str, float] = {}
rr = 0


class Problem(Exception):
    pass


def problem(who: str, what: str) -> None:
    with lock:
        problems.append(f"{who}: {what}")


def http_call(base: str, method: str, path: str, body: dict | None, timeout: float = 60.0) -> tuple[int, dict, float]:
    """One request on a connection of its own. Returns (status, JSON body, Retry-After seconds)."""
    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    try:
        payload = None if body is None else json.dumps(body).encode()
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"error": {"code": "not_json"}}
        try:
            retry_after = float(resp.getheader("Retry-After") or 0.3)
        except ValueError:
            retry_after = 0.3
        return resp.status, data, retry_after
    finally:
        conn.close()


def reachable(base: str) -> bool:
    try:
        http_call(base, "GET", "/healthz", None, timeout=3.0)
        return True
    except (OSError, http.client.HTTPException):
        return False


def pick(bases: list[str]) -> str:
    """The next instance in turn, skipping those that just refused a connection."""
    global rr
    now = time.monotonic()
    with lock:
        for _ in range(len(bases)):
            rr += 1
            base = bases[rr % len(bases)]
            if down_until.get(base, 0.0) <= now:
                return base
        rr += 1
        return bases[rr % len(bases)]  # all marked down: try anyway


def send(bases: list[str], method: str, path: str, body: dict | None, who: str, give_up: float, kind: str) -> dict:
    """Send until answered, the way a client behind a load balancer would."""
    first = time.monotonic()
    me = threading.get_ident()
    while True:
        base = pick(bases)
        with lock:
            inflight[me] = (base, kind)
        try:
            status, data, retry_after = http_call(base, method, path, body)
        except (OSError, http.client.HTTPException) as exc:
            with lock:
                retries[f"connection ({type(exc).__name__})"] += 1
                down_until[base] = time.monotonic() + DOWN_FOR
            status, data, retry_after = 0, {}, 0.1
        finally:
            with lock:
                inflight.pop(me, None)
        if status == 200:
            if kind == "turn":
                with lock:
                    waits.append(time.monotonic() - first)
                    answered_at.append(time.monotonic())
            return data
        code = str((data.get("error") or {}).get("code", "")) if status else ""
        if (status and status not in RETRY_STATUS) or code == "message_id_reused":
            raise Problem(f"{method} {path.rsplit('/', 1)[-1]} answered {status} {code}")
        if status:
            with lock:
                retries[f"{status} {code}"] += 1
        if time.monotonic() - first > give_up:
            raise Problem(f"no answer within {give_up:.0f} s (last: {status or 'no connection'} {code})")
        time.sleep(min(max(retry_after, 0.05), 2.0))


def check_final(who: str, state: dict, messages: list[dict]) -> None:
    def expect(cond: bool, what: str) -> None:
        if not cond:
            problem(who, what)

    said = [m["text"] for m in messages if m["role"] == "user"]
    expect(said == SCRIPT, f"transcript holds {len(said)} caller messages, expected the {len(SCRIPT)} sent, once each, in order")
    ids = [m.get("id") for m in messages if m["role"] == "user" and m.get("id")]
    expect(len(ids) == len(set(ids)), "two caller messages carry the same id")
    expect(state["turn"] == len(SCRIPT), f"turn counter is {state['turn']}, expected {len(SCRIPT)}")
    expect(state["verification"]["verified"], "not verified")
    expect(state["case"]["selected_case_id"] == "CL-2048", f"claim is {state['case']['selected_case_id']}")
    order = [h["to"] for h in state["phase_history"]]
    expect(order == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"], "phases: " + " > ".join(order))
    expect(state["status"] == "completed", f"status is {state['status']}")
    expect(len(state["email"]["outbox"]) == 1, f"{len(state['email']['outbox'])} e-mails in the outbox")


def without_delivery(state: dict) -> dict:
    """The state view without the delivery status of its e-mails and handoff: that changes on its
    own (queued, then sent) after the conversation has ended."""
    out = json.loads(json.dumps(state))
    for entry in (out.get("email") or {}).get("outbox") or []:
        if isinstance(entry, dict):
            entry.pop("delivery", None)
    if isinstance(out.get("escalation"), dict):
        out["escalation"].pop("delivery", None)
    return out


DELIVERED = re.compile(r"demo e-mail transport: ([0-9a-f]{8}):email-0 delivered")


def check_emails(finished: dict[str, dict], base: str, work: Path | None, victim_first_log: Path | None,
                 wait: float) -> dict:
    """Every summary e-mail: queued once and sent (the audit endpoint, polled until each one is
    settled or `wait` runs out: a claim the killed instance held lapses after the dispatcher's
    claim lease), and delivered once (the demo transport's log lines; not with `work` None, when
    the instances were not started here and their logs are out of reach)."""
    effects: dict[str, list[dict]] = {}
    pending = set(finished)
    deadline = time.monotonic() + wait
    while pending:
        for sid in sorted(pending):
            try:
                status, data, _ = http_call(base, "GET", f"/api/sessions/{sid}/audit", None)
            except (OSError, http.client.HTTPException):
                status, data = 0, {}
            if status == 200:
                effects[sid] = [e for e in data.get("effects", []) if e.get("kind") == "email"]
                if effects[sid] and all(e.get("status") in ("sent", "dead") for e in effects[sid]):
                    pending.discard(sid)
        if not pending or time.monotonic() > deadline:
            break
        time.sleep(0.5)

    delivered: Counter = Counter()
    by_victim: Counter = Counter()
    for log in (work.glob("instance-*.log") if work is not None else ()):
        for match in DELIVERED.finditer(log.read_text(encoding="utf-8", errors="replace")):
            delivered[match.group(1)] += 1
            if log == victim_first_log:
                by_victim[match.group(1)] += 1
    sent = twice_after_kill = 0
    for sid in finished:
        who = f"e-mail of {sid[:8]}"
        emails = effects.get(sid, [])
        if len(emails) != 1:
            problem(who, f"{len(emails)} e-mail effects queued, expected exactly 1")
            continue
        effect = emails[0]
        if effect.get("status") != "sent":
            problem(who, f"{effect.get('status')} after {effect.get('attempts')} attempt(s) "
                         f"({effect.get('last_error') or 'no error recorded'})")
            continue
        sent += 1
        if work is None:
            continue
        times = delivered[sid[:8]]
        if times == 2 and by_victim[sid[:8]] == 1:
            twice_after_kill += 1  # at least once: delivered by the killed instance, not recorded
        elif times != 1:
            problem(who, f"delivered {times} times according to the instance logs, expected once")
    return {"sent": sent, "twice_after_kill": twice_after_kill}


def converse(i: int, bases: list[str], give_up: float, finished: dict[str, dict]) -> None:
    global replays
    who = f"caller {i + 1:03d}"
    try:
        sid = send(bases, "POST", "/api/sessions", {"consent_scenario": "default"}, who, give_up, "other")["session_id"]
        turn = 0
        for text in SCRIPT:
            body = {"text": text, "message_id": uuid.uuid4().hex}
            data = send(bases, "POST", f"/api/sessions/{sid}/messages", body, who, give_up, "turn")
            if data.get("replayed"):
                with lock:
                    replays += 1
            if data["state"]["turn"] != turn + 1:
                raise Problem(f"turn counter went {turn} -> {data['state']['turn']} (a message was answered twice or lost)")
            turn += 1
        final = send(bases, "GET", f"/api/sessions/{sid}", None, who, give_up, "other")
        check_final(who, final["state"], final["messages"])
        with lock:
            finished[sid] = final["state"]
    except Problem as exc:
        problem(who, str(exc))
    except Exception as exc:  # noqa: BLE001 - a broken script must not look like a pass
        problem(who, f"{type(exc).__name__}: {exc}")


class External:
    """One instance among those behind --urls, killed and restarted by the commands given."""

    def __init__(self, kill_cmd: str, restart_cmd: str, through: str) -> None:
        self.kill_cmd, self.restart_cmd, self.through = kill_cmd, restart_cmd, through
        self.name = f"an instance ({kill_cmd})"
        self.base = ""  # not addressable by itself

    def _run(self, cmd: str) -> None:
        done = subprocess.run(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False)
        if done.returncode:
            raise Problem(f"`{cmd}` exited with {done.returncode}: {done.stdout.decode(errors='replace').strip()[:300]}")

    def kill(self) -> None:
        self._run(self.kill_cmd)

    def start(self) -> None:
        self._run(self.restart_cmd)

    def wait_healthy(self, seconds: float = 60.0) -> bool:
        """The service as a whole answers "ready" (which instance answers is the balancer's choice)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if http_call(self.through, "GET", "/readyz", None, timeout=5.0)[0] == 200:
                    return True
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        return False


class Instance:
    def __init__(self, port: int, env: dict[str, str], logs: Path) -> None:
        self.port, self.env, self.logs = port, env, logs
        self.base = f"http://127.0.0.1:{port}"
        self.name = f"the instance on port {port}"
        self.proc: subprocess.Popen | None = None
        self.starts = 0

    def start(self) -> None:
        self.starts += 1
        out = open(self.logs / f"instance-{self.port}-{self.starts}.log", "wb")  # noqa: SIM115 - lives with the process
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "insurance_claims", "--mock", "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdout=out, stderr=subprocess.STDOUT,
        )

    def wait_healthy(self, seconds: float = 60.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                if http_call(self.base, "GET", "/healthz", None, timeout=2.0)[0] == 200:
                    return True
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        return False

    def kill(self) -> None:
        """No shutdown handlers, no draining: the process is gone, as in a crash."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", default="", help="shared session store (default: a temporary SQLite file)")
    parser.add_argument("--instances", type=int, default=3)
    parser.add_argument("--callers", type=int, default=60, help="conversations, all started within about a second")
    parser.add_argument("--latency-ms", type=int, default=350, help="delay of each call to the offline double")
    parser.add_argument("--lease", type=float, default=6.0, help="turn lease of the instances, seconds")
    parser.add_argument("--kill-after", type=float, default=3.0, help="seconds after the callers start")
    parser.add_argument("--restart-after", type=float, default=4.0, help="seconds after the kill")
    parser.add_argument("--no-restart", action="store_true", help="leave the killed instance down")
    parser.add_argument("--base-port", type=int, default=8060)
    parser.add_argument("--give-up", type=float, default=90.0, help="seconds a caller keeps trying one message")
    parser.add_argument("--wait-margin", type=float, default=15.0,
                        help="seconds a caller may wait for one answer beyond the turn lease (default 15; "
                             "raise it for instances on the live model, where one turn takes 5-15 s)")
    parser.add_argument("--delivery-wait", type=float, default=150.0,
                        help="seconds to wait for every summary e-mail to be sent (an e-mail claimed by the "
                             "killed instance is free again once the dispatcher's claim lease of 120 s lapses)")
    parser.add_argument("--work-dir", default="", help="folder for the logs and the SQLite file "
                                                       "(default: a new temporary folder)")
    parser.add_argument("--urls", default="", help="comma-separated base URLs of instances that are already "
                                                   "running (one load balancer, or several instances); "
                                                   "nothing is started here")
    parser.add_argument("--kill-cmd", default="", help="with --urls: the command that kills one instance")
    parser.add_argument("--restart-cmd", default="", help="with --urls: the command that starts it again")
    args = parser.parse_args()
    external = [u.strip().rstrip("/") for u in args.urls.split(",") if u.strip()]
    if external:
        if not args.kill_cmd:
            parser.error("--urls needs --kill-cmd, the command that kills one instance")
        if not args.restart_cmd:
            args.no_restart = True
        return run(args, [], external, External(args.kill_cmd, args.restart_cmd, external[0]), None)
    if args.instances < 2:
        parser.error("needs at least two instances: one is killed")

    if args.work_dir:
        work = Path(args.work_dir).resolve() / f"failover-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        work.mkdir(parents=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="sop-failover-"))
    store = args.store or f"sqlite:///{(work / 'sessions.db').as_posix()}"
    env = dict(os.environ)
    env.update({
        "SOP_LLM_MODE": "mock", "SOP_SESSION_STORE": store, "SOP_MOCK_LATENCY_MS": str(args.latency_ms),
        "SOP_TURN_LEASE_SECONDS": str(args.lease), "SOP_WORKERS": "1", "PYTHONUNBUFFERED": "1",
        # Every instance runs a dispatcher; the demo transports log each delivery and send nothing.
        "SOP_DISPATCH": "on", "SOP_EMAIL_TRANSPORT": "demo", "SOP_HANDOFF_TRANSPORT": "demo",
    })
    instances = [Instance(args.base_port + k, env, work) for k in range(args.instances)]
    shown = store.split("@")[-1] if "@" in store else store  # never print a password
    print(f"{args.instances} instances on ports {args.base_port}-{args.base_port + args.instances - 1}, "
          f"store {shown}, turn lease {args.lease:g} s, logs in {work}")
    return run(args, instances, [inst.base for inst in instances], instances[0], work)


def run(args: argparse.Namespace, instances: list[Instance], bases: list[str], victim: Instance | External,
        work: Path | None) -> int:
    """The test itself. `instances`: the ones started here (none with --urls); `bases`: where the
    callers send; `victim`: the instance that is killed."""
    local = bool(instances)
    if not local:
        print(f"callers go to {', '.join(bases)}; assumed turn lease {args.lease:g} s; "
              f"kill: `{args.kill_cmd}`; restart: " + (f"`{args.restart_cmd}`" if args.restart_cmd else "none"))
    try:
        for inst in instances:
            inst.start()
        for inst in instances:
            if not inst.wait_healthy():
                print(f"instance on port {inst.port} did not start; see its log")
                return 2
        stats = http_call(bases[0], "GET", "/api/stats", None)[1]
        if not stats.get("store", {}).get("shared_between_processes"):
            print("the session store is not shared between processes; this test needs one that is")
            return 2

        finished: dict[str, dict] = {}
        threads = [threading.Thread(target=converse, args=(i, bases, args.give_up, finished)) for i in range(args.callers)]
        started = time.monotonic()
        for k, t in enumerate(threads):
            t.start()
            if k % 10 == 9:
                time.sleep(0.15)  # callers arrive within about a second, not in the same millisecond
        time.sleep(max(0.0, args.kill_after - (time.monotonic() - started)))
        with lock:
            hit = sum(1 for base, kind in inflight.values() if kind == "turn" and (not local or base == victim.base))
            before = len(answered_at)
        restarted = False
        try:
            victim.kill()
            killed_at = time.monotonic()
            print(f"t={killed_at - started:.1f} s: killed {victim.name} with {hit} turns in flight "
                  + ("on it" if local else "in all (how many were on it is the balancer's secret)")
                  + f"; {before} turns answered so far")
            if not args.no_restart:
                time.sleep(args.restart_after)
                victim.start()
                restarted = victim.wait_healthy()
                print(f"t={time.monotonic() - started:.1f} s: " + ("it is back" if restarted else "IT DID NOT COME BACK"))
                if not restarted:
                    problem("restart", "the killed instance did not come back")
        except Problem as exc:
            problem("kill/restart", str(exc))
        for t in threads:
            t.join()
        wall = time.monotonic() - started

        # Every instance that is up must show the same finished conversations (but for the
        # delivery status of the e-mail, which moves on by itself) and say it is ready.
        if local:
            up = [inst.base for inst in instances if inst is not victim or restarted]
        else:
            # Given several instances directly, the killed one is among the URLs: left down, it is
            # the one URL that may not answer.
            up = [base for base in bases if reachable(base)]
            if len(bases) - len(up) > (0 if restarted or len(bases) == 1 else 1):
                problem("after the run", f"only {len(up)} of {len(bases)} URL(s) answer")
            if not up:
                raise Problem("no URL answers after the run")
        other = up[min(1, len(up) - 1)]  # an instance that is up (or the balancer)
        ready = 0
        for base in up:
            where = base.rsplit(":", 1)[-1] if local else base
            status, data, _ = http_call(base, "GET", "/readyz", None)
            if status == 200 and data == {"status": "ready"}:
                ready += 1
            else:
                problem(f"port {where}", f"/readyz answered {status} {data}")
            for sid, state in finished.items():
                status, data, _ = http_call(base, "GET", f"/api/sessions/{sid}", None)
                if status != 200:
                    problem(f"port {where}", f"conversation {sid[:8]} answered {status}")
                elif without_delivery(data["state"]) != without_delivery(state):
                    problem(f"port {where}", f"conversation {sid[:8]} differs from what its caller last saw")
        email_start = time.monotonic()
        first_log = work / f"instance-{victim.port}-1.log" if work is not None and isinstance(victim, Instance) else None
        emails = check_emails(finished, other, work, first_log, args.delivery_wait)
        email_wait = time.monotonic() - email_start

        # A message sent twice with the same id is answered once.
        sid = http_call(other, "POST", "/api/sessions", {"consent_scenario": "default"})[1]["session_id"]
        body = {"text": DEMO, "message_id": uuid.uuid4().hex}
        first = http_call(other, "POST", f"/api/sessions/{sid}/messages", body)[1]
        again = http_call(bases[-1], "POST", f"/api/sessions/{sid}/messages", body)[1]
        twice_ok = (again.get("replayed") is True and again.get("reply") == first.get("reply")
                    and again.get("state", {}).get("turn") == 1)
        if not twice_ok:
            problem("same id twice", f"the second send was not a replay (turn counter {again.get('state', {}).get('turn')}, "
                                     f"replayed={again.get('replayed')})")

        wrong = {p.split(":")[0] for p in problems if p.startswith("caller ")}
        total = args.callers * len(SCRIPT)
        seen = sorted(waits)
        print(f"conversations correct: {args.callers - len(wrong)} of {args.callers}; "
              f"{len(seen)} of {total} messages answered in {wall:.1f} s")
        if seen:
            print(f"wait for one answer, first attempt to reply: median {seen[len(seen) // 2]:.1f} s, "
                  f"longest {seen[-1]:.1f} s (turn lease {args.lease:g} s)")
            limit = args.lease + args.wait_margin
            if seen[-1] > limit:
                problem("recovery", f"a caller waited {seen[-1]:.1f} s for one answer, more than the lease plus "
                                    f"{args.wait_margin:g} s")
        print(f"answers that were replays of an already finished turn: {replays}")
        print(f"sent again, by reason: {dict(retries) or 'none'}")
        print(f"same message id sent twice: {'answered once, replayed the second time' if twice_ok else 'WRONG'}")
        if local:
            print(f"finished conversations identical on {len(up)} instance(s)"
                  + (", the restarted one included" if restarted else ""))
            print(f"/readyz: {ready} of {len(up)} instance(s) ready" + (", the restarted one included" if restarted else ""))
        else:
            print(f"finished conversations read back unchanged through {len(up)} URL(s); /readyz ready on {ready}")
        print(f"summary e-mails sent (audit endpoint): {emails['sent']} of {len(finished)}, settled "
              f"{email_wait:.1f} s after the last conversation"
              + (f"; delivered twice because the killed instance delivered one and died before recording it: "
                 f"{emails['twice_after_kill']}" if local else " (deliveries not counted: the instance logs are out of reach)"))
        if problems:
            print(f"\n{len(problems)} PROBLEM(S):")
            for p in problems[:40]:
                print("  " + p)
            return 1
        print("\nALL CORRECT")
        return 0
    except Problem as exc:
        print(f"\nPROBLEM: {exc}")
        return 1
    finally:
        for inst in instances:
            inst.kill()


if __name__ == "__main__":
    sys.exit(main())
