"""Walk the brief requirement by requirement over HTTP, against a running instance in offline mode.

    python scripts/spec_check.py http://127.0.0.1:8000        # one PASS/FAIL line per requirement
    python scripts/spec_check.py http://127.0.0.1:8000 -v     # also print the replies it judged

Standard library only. It reads the session state the API returns and matches a few words of the
offline (mock) replies, so run it against `SOP_LLM_MODE=mock`; the live model words its replies
differently. Exits non-zero if any requirement fails.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8011"
VERBOSE = "-v" in sys.argv
DEMO = ("I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare "
        "claim from January. DOB is 1985-03-15, SSN last four is 4472.")
CLAIM_DETAIL = re.compile(r"CL-\d+|pathology|office note|appeal deadline|March 18|was denied because", re.I)
results: list[tuple[str, bool, str]] = []


def call(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


class Convo:
    def __init__(self, scenario: str = "default") -> None:
        s = call("/api/sessions", {"consent_scenario": scenario})
        self.sid, self.state, self.reply = s["session_id"], s["state"], ""

    def say(self, text: str) -> "Convo":
        r = call(f"/api/sessions/{self.sid}/messages", {"text": text})
        self.state, self.reply = r["state"], r["reply"]
        if VERBOSE:
            print(f"    CALLER: {text}\n    AGENT : {self.reply}\n")
        return self

    @property
    def phase(self) -> str:
        return self.state["phase"]

    @property
    def ver(self) -> dict:
        return self.state["verification"]


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def leaks(text: str) -> bool:
    return bool(CLAIM_DETAIL.search(text))


# 1. VERIFY_ID is strict -------------------------------------------------------------------
c = Convo()
check("Conversation starts in VERIFY_ID", c.phase == "VERIFY_ID")
c.say("Why was my claim denied?")
check("No claim details before verification", c.phase == "VERIFY_ID" and not leaks(c.reply)
      and c.state["case"]["selected_case_id"] is None, f"phase={c.phase}")
c.say("My name is Margaret Chen and my date of birth is 1985-03-15.")
check("Two matching fields are not enough to advance", c.phase == "VERIFY_ID" and not c.ver["verified"]
      and c.ver["matched_count"] == 2, f"matched={c.ver['matched_count']}")
c.say("My phone number is 650-521-2836.")
check("Partial answers add up: third field verifies", c.ver["verified"] and c.ver["matched_count"] >= 3
      and c.phase != "VERIFY_ID", f"matched={c.ver['matched_count']} phase={c.phase}")

c = Convo().say("I'm Margaret Chen. My phone is 650-521-2836 and my email is margaret@email.com.")
check("Alternate identity fields (name + phone + email) verify", c.ver["verified"], f"matched={c.ver['matched_count']}")

c = Convo().say("I'm Margaret Chen, but I'm not comfortable giving my SSN. Is there something else I can use?")
check("Refusal: stays in VERIFY_ID and offers other fields", c.phase == "VERIFY_ID" and not leaks(c.reply)
      and bool(re.search(r"date of birth|phone|email", c.reply, re.I)), f"refusals={c.state['guardrails']['refusals']['count']}")

c = Convo().say("Why do you need my personal information?")
check("Clarification question is answered without advancing", c.phase == "VERIFY_ID" and not leaks(c.reply)
      and bool(re.search(r"protect|privacy|verify|security", c.reply, re.I)))

# 2. Memory across phases ------------------------------------------------------------------
c = Convo().say("I'm calling about my denied healthcare claim from January.")
mem = c.state["memory"]
hints = {h["key"]: h["value"] for h in mem.get("claim_hints", [])}
check("Hint given during VERIFY_ID is stored, phase stays VERIFY_ID", c.phase == "VERIFY_ID" and not leaks(c.reply)
      and mem.get("intent") is not None and len(hints) >= 2, f"hints={hints}")
c.say("My name is Margaret Chen, DOB 1985-03-15, SSN last four 4472.")
check("After verification the remembered hint selects the claim without asking again",
      c.ver["verified"] and c.state["case"]["selected_case_id"] == "CL-2048" and c.phase == "PROCESS_CASE",
      f"claim={c.state['case']['selected_case_id']} phase={c.phase}")

# 3. Demo test case, end to end ------------------------------------------------------------
c = Convo().say(DEMO)
order = [h["to"] for h in c.state["phase_history"]]
check("Demo case: verified in one message", c.ver["verified"] and c.ver["matched_count"] >= 3)
check("Demo case: phases run in order VERIFY_ID > RESOLVE_INTENT > PROCESS_CASE",
      order == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE"], " > ".join(order))
check("Demo case: claim CL-2048 resolved from the remembered hint", c.state["case"]["selected_case_id"] == "CL-2048"
      and "CL-2048" in c.reply)
c.say("How do I submit the missing documents?")
check("PROCESS_CASE: grounded follow-up answer", c.phase == "PROCESS_CASE" and bool(re.search(r"pathology", c.reply, re.I)))
c.say("What is RL?")
check("Out-of-scope question is declined politely, phase unchanged", c.phase == "PROCESS_CASE"
      and c.state["guardrails"]["out_of_scope"]["consecutive"] == 1
      and not re.search(r"reinforcement learning", c.reply, re.I))
c.say("That's all I needed, thanks.")
check("POST_PROCESS: email summary is offered", c.phase == "POST_PROCESS" and c.state["email"]["offered"]
      and bool(re.search(r"email", c.reply, re.I)))
c.say("Yes, please send it.")
outbox = c.state["email"]["outbox"]
body = outbox[0]["body"] if outbox else ""
check("Caller chose send: one email with discussion, status and next steps", len(outbox) == 1
      and all(re.search(p, body, re.I) for p in ("what we discussed", "claim status", "next steps"))
      and c.state["status"] == "completed", f"outbox={len(outbox)} status={c.state['status']}")
order = [h["to"] for h in c.state["phase_history"]]
check("Full workflow order", order == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"], " > ".join(order))

c = Convo().say(DEMO).say("That's all I needed, thanks.").say("No thanks, skip the email.")
check("Caller chose skip: no email sent, conversation completes", len(c.state["email"]["outbox"]) == 0
      and c.state["email"]["decision"] == "skip" and c.state["status"] == "completed",
      f"decision={c.state['email']['decision']} status={c.state['status']}")

# 4. Out of scope, repeated ----------------------------------------------------------------
c = Convo().say(DEMO)
for _ in range(3):
    c.say("What is RL?")
g = c.state["guardrails"]
check("Repeated out-of-scope questions lead to a human-representative offer",
      (g["human_offer_pending"] or c.state["escalation"] is not None)
      and bool(re.search(r"representative|human", c.reply, re.I)),
      f"consecutive={g['out_of_scope']['consecutive']} pending={g['human_offer_pending']}")

# 5. Emotional support and SOP recovery ----------------------------------------------------
c = Convo().say("I already told you who I am. This is ridiculous. Just tell me why my claim was denied.")
emo = c.state["guardrails"]["emotion"]
check("Frustrated caller: acknowledged, gate explained, nothing disclosed", c.phase == "VERIFY_ID" and not leaks(c.reply)
      and emo["label"] != "neutral" and bool(re.search(r"sorry|understand|frustrat", c.reply, re.I))
      and bool(re.search(r"protect|verify|privacy", c.reply, re.I)), f"emotion={emo['label']}/{emo['intensity']}")
check("Frustrated caller: allowed verification options are offered",
      bool(re.search(r"name|date of birth|phone|email|SSN", c.reply, re.I)))
c = Convo().say("I want to talk to a human representative.")
check("Asking for a human is honoured without bypassing the gate", not leaks(c.reply)
      and (c.state["escalation"] is not None or c.state["guardrails"]["human_offer_pending"]
           or bool(re.search(r"representative|transfer", c.reply, re.I))),
      f"status={c.state['status']}")

# 6. Setup ---------------------------------------------------------------------------------
cfg = call("/api/config")
check("Config reports the model mode and whether a key is present", "llm_mode" in cfg and "has_server_key" in cfg,
      f"llm_mode={cfg.get('llm_mode')}")
check("Config lists the four phases", [p.get("id") for p in cfg.get("phases", [])]
      == ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"])

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)} of {len(results)} requirement checks passed")
sys.exit(1 if failed else 0)
