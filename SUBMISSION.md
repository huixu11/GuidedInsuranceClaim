# Submission note — SOP-Guided Conversational Agent

An insurance claims support agent that talks naturally but cannot leave its standard operating
procedure: `VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`.

Everything in the brief is implemented, including the bonus part. This page explains how, in
plain words, and then what the brief does not ask for but a real service needs: how it grows with
the number of callers and what happens when a part of it fails. `README.md` has the full design,
configuration and test details.

## Where to look

| | |
|---|---|
| **Hosted demo** | <https://sop-claims-agent-yw58.onrender.com> |
| **Repository** | <https://github.com/huixu11/GuidedInsuranceClaim> (same content as this zip, plus the CI runs) |
| **This zip** | Source, tests, Dockerfile and docs. No installed dependencies, no secrets. It unpacks to `sop-agent/`. |

About the hosted demo:

- It is on a free plan that sleeps after 15 minutes without traffic, so **the first load can take
  about a minute**.
- **Without a key** it answers with an offline stand-in for the model (fixed, stilted replies).
  That is enough to click through the whole workflow.
- **With your own Anthropic API key** it uses the live model: open **Settings** (the line under
  the message box links to it), paste the key, save. The key stays in that browser tab and is
  sent with each request. The server stores no key.

## How it works, in plain words

### The idea

The workflow is run by ordinary code, not by the language model. The model has only two jobs:

1. **Read** the caller's message and report what it says, as structured data: which identity
   details were given, which claim is meant, what the caller wants, whether the message is on
   topic, how the caller feels.
2. **Phrase** the reply, after the code has decided what this reply must do and which facts it
   may use.

Everything in between is code: checking identity against the records, deciding the phase,
remembering what was said, counting off-topic questions and refusals, calling tools, deciding
whether to send the email. The model cannot change the phase or skip a step, because it is never
the one that decides.

### One turn, step by step

1. **Understand** — the model turns the message into structured data. It decides nothing.
2. **Decide** — code stores anything useful, checks the gates, moves the phase if the rules allow
   it, runs the tools, and produces a *directive*: "the move this reply must make, and the only
   facts it may use".
3. **Respond** — the model writes the reply from that directive, in its own words.
4. **Guard** — code checks the draft (no protected data, no amounts or claim numbers that are not
   in the record). If it fails, the reply is regenerated once, then replaced by a fixed template.

### Why the strict part cannot be talked around

The rule "no claim details before identity is verified" is not a sentence in a prompt that a
persuasive caller could argue with. Before verification:

- no policy or claim data is loaded into the model's context at all,
- the model has no tools,
- and it cannot change the phase.

So an angry or clever caller has nothing to talk the model into: it does not have the data.
"I'm already verified" is just a sentence; only three identifiers matching one record open the
gate, and that check is code.

### Different freedom in different phases

| Phase | The model may | The code enforces |
|---|---|---|
| `VERIFY_ID` (strict) | Word the request, answer "why do you need this?", react to partial answers and refusals | 3 of 5 identifiers must match one record; nothing about the claim is available; wrong values and refusals are counted |
| `RESOLVE_INTENT` (guided) | Interpret messy language and the remembered hints; choose the claim and what the caller needs, or ask | The choice must be one of this caller's own claims and one of six fixed paths |
| `PROCESS_CASE` (open) | Reason and answer follow-up questions freely, using lookup tools | Every lookup checks the claim belongs to the caller; amounts and claim numbers must come from the record |
| `POST_PROCESS` (guided) | Offer the email summary and answer questions about it | The email is sent only after an explicit yes, only to the address on file |

### The brief's test message, as the harness sees it

> I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied
> healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.

1. The model reads it: name, date of birth, SSN last four, policy number, and the hint
   *denied / healthcare / January*.
2. Code stores all of it first, including the hint that belongs to a later phase.
3. Code matches the identifiers against the records: 3 of 3. The gate opens.
4. In the same turn the workflow carries on: the model is shown this caller's claims and the
   stored hint, and picks CL-2048. Code checks that claim really is hers.
5. The claim record is loaded, and the model answers from it. The caller is never asked "what
   are you calling about?", because the agent already knows.

## Is everything in the brief implemented?

Yes. Each line is covered by an automated check (`scripts/spec_check.py` runs them one by one).

| The brief asks for | How it is done |
|---|---|
| Fixed workflow with four phases | The phases, gates, tools and allowed transitions are a data table (`sop.py`) run by a rule engine (`policy.py`). |
| `VERIFY_ID`: no claim details and no progress until at least 3 of 5 identifiers match | Matching is code against one record. Until it passes, the model has no claim data and no tools. |
| Still natural while verifying: clarifications, partial answers, refusals, alternate fields | The model is told the situation (what matched, what is still open, what the caller declined) and words the reply itself. A declined field is not asked for again; the others are offered. |
| Freer reasoning in `RESOLVE_INTENT` and `PROCESS_CASE` | The model chooses the claim and path within fixed bounds, then answers follow-ups in its own words from the claim record and guidance tools. |
| `POST_PROCESS`: offer an email summary; the caller chooses send or skip | Always offered. The summary lists what was discussed, the claim status and the next steps. Nothing is sent without an explicit yes. |
| Refuse out-of-scope questions politely; a human after repeated tries | Scope is read per message and counted in code: polite refusal, a human representative offered on the third in a row, transfer on the fourth. |
| Remember useful information whenever it is said | Stored on every turn, whatever the phase, with the turn it came from. The agent stays in `VERIFY_ID` and uses the hint right after verification. |
| The demo test case | Behaves as the brief describes (see the walk-through above). |
| Bonus: emotion and SOP recovery | Emotion and refusals are read each turn and shape the reply: acknowledge first, explain why the step exists, offer other identifiers or a human, stop persuading after two attempts and hand off. No gate is relaxed to calm a caller down. |
| Delivery 1: hosted demo, or Docker image / repository with setup instructions | All three: the hosted demo above, a Dockerfile, and the repository with a README. |
| Delivery 2: setup accepts an API auth token | Three ways: the `ANTHROPIC_API_KEY` environment variable, a `.env` file, or the UI's Settings dialog. |
| Delivery 3: a simple test UI for chatting in natural language | A browser UI: chat on the left, the harness's state on the right. |
| Delivery 4: the demo shows the full workflow | Four suggested replies walk through all four phases; the steps above the chat show the progress. |

## Many callers, and things going wrong

The brief does not ask for this. But a harness that works for one caller on one machine that
never fails is a toy, so this is how the service is built to grow and to survive failures, what
was measured, and what is still only a design. `README.md` → *Scale and failure: the design* and
*Scaling and limits* have the detail.

### The design in six rules

```
callers ─► load balancer ─► instance 1..N ─► PostgreSQL ◄─ dispatcher ─► e-mail, human-agent desk
                                 │
                                 ▼
                             model API
```

1. **An instance holds nothing.** Everything a conversation needs between two messages is in the
   database. Any instance can answer any message, so adding instances adds capacity and losing
   one loses no conversation.
2. **A turn is all or nothing.** It works on a private copy and saves the new state, the new
   transcript lines, the audit events and the e-mails to send in one transaction.
3. **One turn at a time per conversation, held by a lease that expires.** An instance that dies
   stops renewing its leases, so its conversations are free again within 20 seconds. Nobody has
   to clean up after it.
4. **Every message can be sent again.** Each message carries an id. If it was already answered,
   the stored answer comes back and nothing runs twice. So whatever went wrong on the way, the
   client's remedy is always the same: send it again.
5. **E-mails and handoffs go through a queue.** They are written with the turn and delivered
   afterwards, with retries. A mail system that is down cannot fail a turn, a crash cannot lose
   an e-mail, and no e-mail goes out for a turn that was not saved.
6. **Every wait has a limit.** When a limit is reached the caller is told when to come back
   (`Retry-After`), instead of joining a queue that only grows.

### What happens when something fails

| What fails | What happens | What the caller sees |
|---|---|---|
| An instance dies in the middle of a turn | Nothing of that turn was saved; its lease expires within 20 s | The message is sent again and another instance answers it |
| An instance dies after saving, before the answer arrives | The turn is in the database | The resent message gets the stored answer; the turn does not run twice |
| An instance is stopped for a deploy | It stops taking new requests, finishes the turns it has, then stops | Nothing, or one automatic retry |
| The database is unreachable | Nothing is half-saved; instances report "not ready" and reconnect by themselves when it is back | "Busy, try again in a moment" |
| The model service is slow or down | Each call has a timeout and each turn a time budget; after five unanswered calls in a row, calls are refused at once for 15 s instead of piling up | Before the decision: "try again shortly", nothing recorded. After it: the fixed template wording, so a decision is never lost |
| The mail system or the agent desk is down | The delivery waits in the queue and is retried for minutes, then marked failed and visible to operators | The conversation carries on |
| More callers than capacity | Turns beyond the limit are refused quickly | "Busy", and the UI retries by itself |

Each row is covered by an automated test. The first is also what `scripts/failover_test.py`
does for real: it kills one of three running instances in the middle of a run and checks every
transcript.

### Single points of failure, honestly

| Part | Redundant here? |
|---|---|
| Application instances | **Yes.** `docker-compose.ha.yml` runs three behind a load balancer; CI kills one mid-run. |
| Delivery of e-mails and handoffs | **Yes.** Every instance runs a dispatcher; they share the queue safely. |
| Database | **No: one PostgreSQL.** In production this is a managed PostgreSQL with a standby and automatic failover. The application's side of that is built and tested (it survives the database going away and coming back, without a restart). A failover between two database nodes was not tested. |
| Load balancer | **No** in the compose file (one container). In production it is the platform's. |
| Model provider | **No.** An outage is contained, but new callers cannot be verified while it lasts. |

### What was measured

On one laptop, with the server, the database and the load generator all on that machine, and the
offline stand-in for the model. `scripts/load_test.py` plays the callers and checks every
conversation, not just status codes: each one must end where its script says, and no reply may
contain another policyholder's claim.

| Setup | Callers (at once) | Conversations correct | Result |
|---|---|---|---|
| One instance, conversations in memory | 400 (100) | 400 of 400, in both runs | 186–212 turns/s |
| Two instances sharing one SQLite file | 400 (100) | 400 of 400, in both runs | 197–201 turns/s; each conversation alternated between the two |
| Two instances sharing one PostgreSQL database | 400 (100) | 400 of 400, in both runs | 120–129 turns/s; each conversation alternated between the two |
| Model-like latency (about 3–4.5 s a turn), within the limit | 120 (60) | 120 of 120 | 13 turns/s, nothing refused |
| Model-like latency, over the limit | 200 (150) | 200 of 200 | 17 turns/s; 312 messages were told to come back, and all were answered on retry |

**An instance killed in the middle** (`scripts/failover_test.py`): three instances on one store,
60 callers in mid-conversation, one instance killed outright after three seconds and started
again (it was back ten seconds after the kill).

| Store | Conversations correct | Messages answered exactly once | Longest wait for one answer | Summary e-mails sent, once each |
|---|---|---|---|---|
| SQLite | 60 of 60 | 300 of 300 | 9.7 s | 60 of 60 |
| PostgreSQL | 60 of 60 | 300 of 300 | 9.7 s | 60 of 60 |

About 20 turns were in flight on the instance when it died. Their callers got a broken
connection, sent the same message again, and were answered by another instance once the dead
instance's lease had run out (6 s in this test, 20 s by default). Afterwards all three instances,
the restarted one included, returned identical conversations.

Also checked: eight messages sent at the same instant to one conversation are answered one at a
time, in order; the same message sent twice is answered once; malformed and oversized requests
sent to a running server were refused cleanly and changed nothing.

**With the live model** (same laptop, default models, on this version), smaller checks, because
every turn costs credit:

| Setup | Conversations at once | Conversations correct | Result |
|---|---|---|---|
| One process, conversations in memory (`scripts/live_check.py`) | 12 | 12 of 12 | 60 turns in 35 s (392 s one after another); 6.8 s a turn at the median, the same as one caller alone |
| Three instances on one PostgreSQL, one killed while it was waiting for the model (`scripts/failover_test.py`) | 8 | 8 of 8 | 40 of 40 messages answered once. The three turns that died with the instance were answered by another one, 18.6 s after the first attempt at the longest; 8 of 8 e-mails sent |

No model errors and no rate limiting in either. Four messages sent at the same instant to one
conversation were answered one at a time, none lost. A conversation taken to its message cap was
handed to a human and then closed. With the model's address made unreachable, the real SDK
client gave up after its timeout, callers were told "come back in 15 seconds" at once after five
failed calls, and the conversation was left untouched.

### So how many callers?

With the live model, one process keeps 64 turns in flight. At the measured 4.4 to 6.8 seconds a
turn that is 9–15 turns a second. A caller sends a message every 20–30 seconds, so that is
roughly **200–400 callers in conversation at the same time per process**, provided the model
account allows about 25–50 requests a second. Instances add up, since they share nothing but the
database.

The ceiling is the model account, not the harness: 10,000 callers in conversation at once would
be about 400 messages and 1,000 model requests a second. The database is far from being the
limit at that point (two instances on one PostgreSQL on a laptop answered 120–130 turns a
second with the offline stand-in).

These figures are worked out from measured turn times. The live check went to 12 conversations
at once: a live load test up to the limit was not run.

### What it still does not do

- **Survive the loss of its database node.** That needs a standby and automatic failover, which
  a managed PostgreSQL provides and this repository does not set up.
- **Use a second model provider** when the first is down.
- **Authenticate callers or apply global quotas.** That belongs in a gateway in front; the
  built-in rate limit is per instance.
- **Talk to a real mail system or agent desk.** The delivery queue, an SMTP sender and a webhook
  sender are built and tested against local stand-ins; the systems behind them are simulated,
  as the brief's own fixtures simulate the consent service.
- **Export metrics and alerts.** `/api/stats` and the logs are what there is.
- The hosted demo is one small free instance with conversations in memory. It shows the
  workflow; the multi-instance setup is what CI runs.

## Run it locally

```bash
# Docker
docker build -t sop-agent .
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... sop-agent

# or Python 3.11+
python -m venv .venv && .venv/bin/pip install -r requirements.txt
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python -m insurance_claims

# or with no key at all (offline stand-in for the model)
python -m insurance_claims --mock
```

Then open <http://localhost:8000>.

```bash
# three instances behind a load balancer on PostgreSQL; kill one and carry on
docker compose -f docker-compose.ha.yml up --build --wait      # http://localhost:8080
docker kill sop-ha-app2-1
```

## A two-minute demo

1. Click the suggested reply marked with an arrow (**Demo test case**) and press Enter.
2. Keep clicking the arrow reply: a document question, a wrap-up, then *send* for the email
   summary. Four messages, four phases. The **SOP state** pane on the right shows what the
   harness decided each turn; its **Trace** tab has the detail.
3. Start a new conversation and try the other suggestions: **Frustrated caller** before
   verifying, **Out of scope** three times, **Skip the summary**.

**Test data** in the top bar lists the fixture identities and claims, so you know what to type.

## What was checked

- **887 automated tests**, all offline. They include 150 random 30-turn conversations that
  feed the engine arbitrary, even hostile, readings and assert after every turn that nothing
  behind the gate was touched before verification and that the audit trail contains nothing a
  caller said; malformed and extreme HTTP input, which must never cause a server error or change
  a conversation; the three stores (memory, SQLite, PostgreSQL) against one contract, including a
  killed process, a database that goes away and comes back, and encryption at rest; retried and
  duplicated messages; deliveries against local mail and webhook receivers that fail, hang and
  recover; and the model service failing in each way it can.
- **A load test** (`scripts/load_test.py`) that checks every simulated conversation, and **a
  failover test** (`scripts/failover_test.py`) that kills an instance mid-run; results in *Many
  callers, and things going wrong*.
- **24 requirement checks** (`scripts/spec_check.py`), one per line of the brief. All 24 pass
  against the offline stand-in. Against the live model 23 passed as written; the 24th failed on
  its keyword list, not on the behaviour: the agent explained why it verifies in words the list
  did not have (the list was widened afterwards). On the previous version they also passed
  against the hosted demo.
- **Against the live model, on this version**: ten scripted conversations
  (`scripts/run_scenarios.py`), twelve callers at once (`scripts/live_check.py`) and an instance
  killed in the middle of live turns (`scripts/failover_test.py`); results in *Many callers, and
  things going wrong*.
- **CI on every push** (GitHub Actions): builds the Docker image, runs it, plays the demo case
  through it and load-tests it; runs the test suite next to a real PostgreSQL and the failover
  test on SQLite and PostgreSQL; and starts three containers behind a load balancer on
  PostgreSQL, kills one in the middle of a run, then stops and starts the database.

## Limits worth knowing

- The mail system, the human-agent desk and the policyholder-consent service are simulated, as
  the brief's own fixtures simulate consent. E-mails and handoffs do go through a real delivery
  queue, and senders for SMTP and for a webhook are included; they were tested against local
  stand-ins only.
- Conversations are held in memory by default (lost on restart); a SQLite file or a PostgreSQL
  database can be configured instead. See *Many callers, and things going wrong* for what is and
  is not there for scale and failures. The database is the one single point of failure left.
- The offline stand-in understands language crudely (regular expressions). It is there to show
  the control flow without a key; judge the conversation quality with the live model.
- The UI was checked in desktop Chrome and at phone width.
- `README.md` → *Assumptions and choices worth knowing* lists the rest, including the demo clock
  (the agent reasons as if today were 2026-03-02, to match the dates in the fixtures).
