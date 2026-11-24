# SOP-Guided Conversational Agent — Insurance Claims Support

> Reviewing this as a submission? [SUBMISSION.md](SUBMISSION.md) is the one-page cover note: the
> hosted demo, how to run it, a two-minute demo and how the brief is met.

A claims-support agent that talks like a person but cannot leave the standard operating
procedure. A deterministic harness owns the workflow
(`VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`), the safety gates, the memory and
the allowed actions; the LLM reads what the caller said and phrases what the harness has
decided. How much freedom the LLM gets is set per phase.

The core idea: **the strict parts are enforced by structure, not by prompt.** Before identity is
verified, no policy or claim data is loaded into the model's context at all, the model has no
tools, and it cannot change the phase — so there is nothing for a persuasive or upset caller to
talk it into.

![The test UI mid-conversation: the chat under the four workflow steps, the SOP state on the right](docs/ui-process-case.png)

---

## Quick start

You need an Anthropic API key (the "API auth token") for the model. It can be supplied in any of
three ways: the `ANTHROPIC_API_KEY` environment variable, a `.env` file (copy `.env.example`),
or pasted into the UI's **Settings** dialog (kept in the browser tab, sent per request as an
`X-API-Key` header — convenient for a hosted demo where each tester brings a key; with
`SOP_LLM_MODE=auto` such a demo also answers offline for testers who have none).

### Docker

```bash
docker build -t sop-agent .
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... sop-agent
# or:  ANTHROPIC_API_KEY=sk-ant-... docker compose up --build
```

Open <http://localhost:8000>.

Three instances behind a load balancer on one PostgreSQL database, to see that any instance can
be lost ([Scale and failure](#scale-and-failure-the-design)):

```bash
docker compose -f docker-compose.ha.yml up --build --wait     # then open http://localhost:8080
docker kill sop-ha-app2-1                                     # carry on with the conversation
```

### Local Python (3.11+; developed and tested on 3.14)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
export ANTHROPIC_API_KEY=sk-ant-...                # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
.venv/bin/python -m insurance_claims               # Windows: .venv\Scripts\python -m insurance_claims
```

### No key? Offline mode

`SOP_LLM_MODE=mock` replaces the model with a regex/template test double. Replies are stilted and
its language understanding is crude, but every harness mechanism (gates, memory, transitions,
tools, escalation, guard) runs exactly as in model mode, so it is useful for seeing the control
flow and for CI.

```bash
python -m insurance_claims --mock          # same as SOP_LLM_MODE=mock; --host / --port also accepted
docker run --rm -p 8000:8000 -e SOP_LLM_MODE=mock sop-agent
```

### Hosted demo

`render.yaml` is a [Render](https://render.com) Blueprint that builds the same Dockerfile and
serves it at a fixed `https://<name>.onrender.com` address, on the free plan:

1. Sign in to Render with GitHub. When GitHub asks which repositories the Render app may access,
   *Only select repositories* with this one is enough (it can be changed later at
   <https://github.com/apps/render/installations/new>).
2. In the Render Dashboard choose **New → Blueprint**, click **Connect** next to this repository,
   keep the `main` branch, review the one web service it lists and click **Deploy Blueprint**. The
   first build takes a few minutes.

The Blueprint sets `SOP_LLM_MODE=auto` and stores no key on the server:

- **without a key** the offline test double answers, so anyone can click through the whole
  workflow (the header says *offline mock model* and the Settings button reads **Add API key**);
- **with the tester's own key**, saved in **Settings**, that browser tab talks to the live model
  using that key. It is kept in the tab's `sessionStorage` and sent with each request; the server
  does not store it. Clearing it goes back to offline, even mid-conversation.

So the hosted demo never spends the owner's credit. To make it always use the live model instead,
set `SOP_LLM_MODE=anthropic` in the service's **Environment** tab (testers without a key are then
asked for one), optionally with `ANTHROPIC_API_KEY` as a secret there.

Worth knowing about the free plan: the service stops after 15 minutes without traffic and takes
about a minute to wake on the next request, and conversations live in server memory, so they
start afresh after a wake-up or a redeploy. Every push to the default branch redeploys. It is one
small instance (512 MB, a fraction of a CPU), there to show the workflow rather than to carry
load; the Blueprint lowers the conversation caps to fit that memory
([Scaling and limits](#scaling-and-limits)).

---

## Using the test UI

The chat is on the left, with the four SOP phases as steps above it (done, current, still to
come; after a handoff it stops at the phase where the call was transferred). On the right, the
**SOP state** shows what the harness is doing, in three tabs:

- **Overview** — the current phase and how much freedom the agent has in it (strict / guided /
  open); **identity**: which of the five identifiers matched, failed attempts, the caller's role,
  representative and consent status (identifier *values* are never shown or logged); what the
  agent **remembered**, with the turn it was captured in; the selected **claim** and workflow
  path, locked until identity is verified; the **safeguards** (out-of-scope strikes, refusals,
  caller mood, human handoff); and the **summary email**.
- **Trace** — the phase history, then for every turn: the structured reading, memory writes, gate
  decisions, tool calls, phase transitions, the directive given to the model, model calls and
  guard results. *Turn N* and *Show in chat* scroll the chat to that turn.
- **JSON** — the raw session state, with a copy button.

Values that changed in the latest turn are briefly highlighted. On a narrow screen the chat and
the SOP state are two tabs, and a dot on **SOP state** says it has changed.

**Suggested replies** sit in one row above the message box and change with the phase. The first,
marked with an arrow, carries the demo forward, so the whole workflow can be walked by clicking it
and pressing Enter four times; the next few are the detours most worth trying at that point (a
frustrated caller, a partial answer, an off-topic question, a request for a human). Clicking one
fills the message box so it can be edited before sending. **More** lists every suggestion by phase.

When the caller chooses to have the summary email sent, it appears in the chat under the agent's
reply, as well as in the Overview, with its delivery status (queued, then sent, or not delivered);
a handoff to a human shows its status the same way. A conversation survives a page reload, and
`?session=<id>` opens an existing one (with the default in-memory store a server restart starts
fresh; with a SQLite or PostgreSQL store it does not).

![The last step: the summary email shown in the chat after the caller chose to send it](docs/ui-email-summary.png)

**Test data** (top bar) lists the fixture identities and claims so you know what to type.
**Settings** holds the model API key (when the server runs the live model and has no key of its
own, the button reads **Add API key**), the **consent scenario** — how the simulated
policyholder-consent service behaves for the next new conversation (`default`: approved on the
second check; `timeout`: never approved) — and the date the demo clock is set to.

### A five-minute walkthrough

Start a new conversation for each block. Most of these messages are among the suggested replies.

1. **The demo test case** — one message with identity, policy number and the reason for calling:
   > I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied
   > healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.

   The **Trace** tab shows `verify_identity → 3 of 3`, the hint (*denied / healthcare /
   January*) stored during `VERIFY_ID`, the resolver selecting CL-2048 from it, and the reply
   picking up from "you mentioned…" instead of asking what the call is about. Then ask *"How do I
   submit the missing documents, and how long will the review take?"* (a tool call to the
   guidance data), say *"That's all, thanks"* (email offer) and answer *send* or *skip*.
2. **Memory across phases** — say *"Hi, I'm calling about my denied healthcare claim from
   January."* first. The agent stays in `VERIFY_ID`, discloses nothing, and stores the hint. Give
   name + date of birth, then phone: on verification it goes straight to the right claim.
3. **Emotional recovery** — give only a name, then *"I already told you who I am. This is
   ridiculous. Just tell me why my claim was denied."* Empathy first, the reason for verification,
   the options, no claim details. *"I'm not comfortable giving my SSN"* → other identifiers are
   offered and the SSN is not asked for again. Keep refusing outright and the agent stops
   persuading after two attempts and hands off to a human.
4. **Scope** — *"What is RL?"* is declined and steered back; on the third consecutive off-topic
   message a human is offered, on the fourth the conversation is transferred.
5. **Wrong details** — a wrong date of birth is reported as not matching (never what the right
   value is); three failed attempts lock verification and hand off.
6. **Representative** — *"I'm David Chen, calling on behalf of my mother Margaret Chen…"* with her
   date of birth and phone: identifiers match, David is an authorised representative on file, and
   a consent request goes to the policyholder; details unlock only when consent is approved. Try
   it again with **Settings → Consent scenario** set to `timeout`.

---

## How it works

### One turn

```
caller message
   │
   ▼  1. UNDERSTAND   model call, structured output. Extracts identifiers, claim hints, intent,
   │                  scope, emotion, refusals, yes/no… It reports what was said; it decides nothing.
   ▼
   2. DECIDE          policy.step() — plain code, no model:
   │                    • remember everything useful (whatever the phase)
   │                    • scope / emotion / refusal counters, escalation rules
   │                    • identity matching against records; consent checks
   │                    • phase gates and transitions (can cascade: verify → resolve → process)
   │                    • harness-run tools (verify_identity, list_claims, get_claim_details, …)
   │                    • one bounded model decision, validated: which of the caller's own claims,
   │                      which workflow path (RESOLVE_INTENT)
   ▼
   Directive          the move this reply must make + the only facts the model may use
   │
   ▼  3. RESPOND      model call. Sees: fixed persona/rules, a control block rendered from the
   │                  directive, the transcript, and only the tools the phase allows.
   ▼
   4. GUARD           deterministic checks on the draft: no protected data, no ungrounded amounts
   │                  or claim numbers. A failed check regenerates once or falls back to a template.
   ▼
reply
```

Every directive also has a fixed template wording (`replies.py`). It is used when the guard
rejects a reply, when a model call fails or is refused, and in mock mode — so the SOP can always
make its move.

**When a model call fails** depends on where the decision is made. Before step 2 nothing has
been decided: if the reading is unusable (truncated or unparseable, after one retry) or the
claim-matching call fails, the turn is abandoned, nothing is recorded, and the UI offers Retry —
the harness never acts on a message nobody understood. After step 2 the move is decided and its
tools have run, so a failure while phrasing never rolls the turn back: the template wording is
sent. If the model or a safety classifier *declines* a message, it is treated as out of scope.

### The freedom dial

| Phase | Freedom | What the LLM does | What the harness enforces | Data in model context | LLM tools |
|---|---|---|---|---|---|
| `VERIFY_ID` | **strict** | Phrases the harness's request; handles questions, pushback, partial answers | 3-of-5 identifier match against one record; mismatch strikes; refusal ladder; representative lookup and consent; exit gate | none — only "matched / not matched / still available" by field name | none |
| `RESOLVE_INTENT` | **guided** | Interprets messy language and remembered hints; picks a claim and workflow path, or asks | Choice must be one of the verified policyholder's claims and one of six paths, else rejected | claim index (number, type, date, status, one-line summary) | none |
| `PROCESS_CASE` | **open** | Reasons and answers follow-ups in its own words | Claim ownership on every tool call; grounding check on amounts and claim numbers | full record of the selected claim + tool results | `get_document_guidance`, `list_claims` |
| `POST_PROCESS` | **guided** | Offers the email summary, answers questions about it | Email sent only on an explicit yes, only to the address on file; default is not to send | conversation + summary | none |

The phase table itself is data (`sop.py`): label, freedom, disclosure level, harness tools, LLM
tools, legal transitions and entry gates. `check_transition` and `check_tool` are called on every
transition and tool call and raise if the SOP does not define what is being attempted.

Legal transitions: `VERIFY_ID → RESOLVE_INTENT`, `RESOLVE_INTENT → PROCESS_CASE | POST_PROCESS`,
`PROCESS_CASE → RESOLVE_INTENT | POST_PROCESS`, `POST_PROCESS → RESOLVE_INTENT | PROCESS_CASE`.
Nothing leads back to, or around, `VERIFY_ID`. A conversation ends as `completed` or `escalated`.

### Requirement → mechanism

| Requirement | How it is met | Where |
|---|---|---|
| No disclosure or progress before ≥3 PII match | Matching is code against one record; entry gate on every later phase; record data is not fetched or put in the prompt before then; guard as tripwire | `identity.py`, `sop.py`, `policy._handle_verify`, `guard.py` |
| Natural conversation during verification: clarifications, partial answers, refusals, alternate fields | The control block gives the model the situation (matched / mismatched / available / declined) and the move; wording is the model's | `prompts._section`, `policy._identity_facts` |
| Freer reasoning in RESOLVE_INTENT / PROCESS_CASE | Bounded resolver decision; grounded free-form answers with a tool loop | `policy._handle_resolve`, `llm.respond`, `tools.py` |
| Remember information said early | `_remember` runs before any phase logic, every turn; items carry the phase/turn they were captured in | `policy._remember`, `state.py` |
| Use remembered hints after verification | Phases cascade within a turn; the resolver receives the hints; the reply is told to pick up from them | `policy.step`, `prompts.resolve_user_prompt` |
| In-scope only; polite refusal; human after repeated off-topic | Scope classified per message; strikes counted in code; offer at the limit, transfer after it | `policy._track_guardrails` |
| Email summary with send / skip | Always offered on entering POST_PROCESS (a preference stated earlier in the call is remembered and mentioned, and the caller still chooses); explicit choice required; summary drafted from records and transcript; queued with the turn and delivered by the dispatcher (a demo transport by default, SMTP optional) | `policy._handle_post`, `tools.send_summary_email`, `dispatch.py` |
| Emotion, empathy, explaining why, alternatives, knowing when to stop | Emotion and refusal tracked per turn; empathy / explain-why / offer-human modifiers; persuasion ladder with a hard stop | `policy._handle_verify`, `prompts._modifier_lines` |

### Verification rules

- Five identifiers count: full name, date of birth, phone, email, SSN / national ID last four.
  Any three matching **the same record** verify the caller, exactly as the SOP states.
  Identifiers from different people never add up. A further identifier that does not match is
  a counted strike, but it does not undo three that do.
- The policy number helps locate the record but does not count toward the three.
- Values are normalised in code (date spellings, phone punctuation and extensions, name order,
  accents and spacing, aliases from the fixtures such as `Yaven Li`). Ambiguous values (two
  unrelated numbers given as "last four") are rejected rather than guessed at.
- A mismatch is reported by field name only, never the value on file. Each turn that introduces
  a new non-matching value is a strike; three strikes hand off to a human. Repeating the same
  wrong value is not a new strike, and a corrected value can still verify.
- Declining a particular identifier is fine: it is not asked for again and the others are
  offered. If too few remain to reach three, a human is offered.
- A caller who refuses outright gets two attempts at persuasion, then a handoff. A refusing
  turn only escapes being counted if it actually moves verification forward (a newly matched
  identifier) or earns a strike, so the conversation cannot be stalled indefinitely.
- **Representatives**: the caller must supply three of the *policyholder's* identifiers, be listed
  in `representatives.json` for that policyholder, and the policyholder must approve a consent
  request (simulated from `consent_scenarios.json`). Authorisation and consent are bound to the
  (policyholder, representative) pair: changing either re-runs the check, an approval is never
  inherited, and a request that timed out is not restarted. Any sign of a third party (a stated
  role, a representative's name, a relationship) puts the call on this path and it stays there.
  An unlisted representative, or a consent timeout, keeps everything locked and offers a human.
- The limits are configurable, but only upward: fewer than three matching identifiers cannot be
  configured.

### Emotional support and SOP recovery

Each message is read for emotion (label and intensity), refusal type, "why do I have to?", and a
request for a human. The harness turns that into instructions layered on the phase's move:

| Signal | Effect |
|---|---|
| Upset / anxious / confused | Reply opens with a specific acknowledgement before any request |
| "Why do you need this?" or pushback | One-sentence reason in terms of protecting the caller |
| Declines one identifier | That field is dropped; alternatives offered |
| Refuses verification (1st time) | Empathy + reason + "you choose which three" |
| Refuses again (2nd) | Short, respectful last attempt: continue, or a human representative |
| Refuses a third time | Stop persuading; transfer to a human |
| Strong frustration three turns running | A human is offered (not forced) |
| Asks for a human, or clearly accepts an offer | Transferred immediately, at any phase |
| Says just "yes" after a human was offered alongside another question | Nothing is assumed (no transfer, no email): the reply asks which was meant |

A transfer produces a handoff summary for the human representative (reason, phase, verification
progress, the caller's stated need, emotional state) and tells the caller that identity will
still be verified. No gate is ever relaxed as part of de-escalation.

### Defence in depth

1. **Structure** — before verification there is no record data in the prompt and no tools.
2. **Separation of duties** — the model that reads the message cannot act; the code that acts
   does not take instructions from text. "I'm already verified" is just a sentence.
3. **Untrusted readings** — the structured reading is treated as hostile input. Free text in it
   is reduced to one short plain line (no markup, no line breaks) before it is stored, and where
   it is shown to the reply model it is marked as the caller's unverified words, so it cannot
   pose as part of the instructions. The engine's invariants are tested against arbitrary
   readings (see Tests).
4. **Bounded choices** — the resolver's pick is validated against the caller's own claims.
5. **Tool scoping** — per-phase allowlists for harness and model; every claim lookup checks
   ownership; the email tool refuses without recorded consent.
6. **Output guard** — on every model-written reply *and on the drafted email summary*: blocks
   other people's data, stored identifiers being read back, claim data before verification,
   statements that the caller is verified when they are not, dollar amounts (however written)
   that are not in the record or tool results — including a figure the caller merely mentioned —
   and unknown claim numbers. A rejected summary is rebuilt from the claim records.
7. **Fail safe** — any model failure or refusal falls back to the template for the same directive.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Model API auth token (or `ANTHROPIC_AUTH_TOKEN`; or paste a key in the UI) |
| `SOP_LLM_MODE` | `anthropic` | `anthropic` (the model; a key is required), `mock` (offline test double) or `auto` (the model when a key is available — the tester's or the server's — otherwise the offline double) |
| `SOP_MODEL` | `claude-opus-5-5` | Model that phrases replies |
| `SOP_MODEL_UNDERSTAND` | `claude-haiku-5-5` | Model for the structured calls (understand / resolve / summarize); set it to the `SOP_MODEL` value to use one model for everything |
| `SOP_EFFORT`, `SOP_EFFORT_UNDERSTAND` | `low` | Reasoning effort per call type |
| `SOP_REFUSAL_FALLBACK` | `auto` | Server-side refusal fallback opt-in (`auto` = on for models that support it) |
| `SOP_TODAY` | `2026-03-02` | The date the agent reasons with; `real` uses the wall clock |
| `SOP_REQUIRED_ID_MATCHES` | `3` | Identifiers that must match |
| `SOP_MAX_FAILED_ATTEMPTS` | `3` | Mismatch strikes before handoff |
| `SOP_OUT_OF_SCOPE_LIMIT` | `3` | Consecutive off-topic messages before a human is offered |
| `SOP_MAX_PERSUASION_ATTEMPTS` | `2` | Attempts to persuade a caller who refuses verification |
| `SOP_EXPOSE_FIXTURES` | `1` | Show fixture data in the UI's Test data drawer |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Bind address (the Docker image binds `0.0.0.0`) |
| `SOP_SESSION_STORE` | `memory` | Where conversations live: `memory` (this process); a SQLite file (`sqlite:///path/sessions.db` or a plain path) shared by the workers and instances on one machine; or `postgresql://user:password@host:5432/db`, shared by instances on any number of machines |
| `SOP_WORKERS` | `1` | Worker processes (same as `--workers`); more than one needs a shared store |
| `SOP_DATA_KEY` | — | 32 random bytes (base64 or hex): encrypts transcripts, state, traces and queued e-mails at rest in the SQLite and PostgreSQL stores |
| `SOP_EMAIL_TRANSPORT`, `SOP_HANDOFF_TRANSPORT` | `demo`, `demo` | How the summary e-mail and the handoff to a human leave: `demo` (nothing leaves the machine) or `smtp` / `webhook` ([Delivery settings](#delivery-settings)) |
| `SOP_MAX_CONCURRENT_TURNS` | `64` | Turns processed at once per process; beyond that a turn waits briefly, then gets `503` |
| `SOP_MAX_TURNS_PER_CONVERSATION` | `60` | Caller messages per conversation; the last one is answered by a handoff to a human |
| `SOP_MAX_SESSIONS`, `SOP_MAX_STORE_MB`, `SOP_SESSION_TTL_SECONDS` | `50000`, `1024`, `14400` | Conversations held, state held in memory (memory store), idle expiry |
| `SOP_RATE_LIMIT_PER_MINUTE`, `SOP_TRUST_PROXY` | `0` (off), `0` | Optional per-client limit, and whether the client is the last `X-Forwarded-For` hop |
| `SOP_MOCK_LATENCY_MS` | `0` | Offline double only: time each simulated model call takes, for load tests |

The serving settings, the remaining waits and what each limit answers are described in
[Scaling and limits](#scaling-and-limits); how the service is built to grow and to survive
failures is in [Scale and failure: the design](#scale-and-failure-the-design).

A turn makes two to four model calls (understand; resolve on the turn a claim is selected;
respond; one more if the model calls a tool). The structured calls are short extraction and
classification requests, so by default they go to the smaller model while the larger one
phrases the replies. Measured on the ten scripted scenarios with the defaults (low effort):
typically 3–5 s per turn, and 6–9 s for a turn that verifies, selects the claim and answers in
one go or that calls a tool. With `claude-opus-5-5` for everything the scenarios reached the
same outcomes at typically 5–7 s per turn (10–13 s for the heaviest turns).

## Scale and failure: the design

The harness above decides what the agent may say and do. This section is about the other half of
a service: many callers at once, and what happens when a part of it breaks. The short version:

```
                              ┌─ instance 1 ─┐
callers ─► load balancer ─────┼─ instance 2 ─┼────► PostgreSQL ◄──── dispatcher (a thread in
           (uses /readyz)     └─ instance N ─┘      conversations     every instance) ──► e-mail,
                                     │              messages, traces                      human-agent
                                     ▼              audit_events                          desk
                                 model API          outbox
```

Six rules, and everything else follows from them:

1. **An instance holds nothing.** Whatever a conversation needs between two messages is in the
   store. Any instance answers any message of any conversation, so there are no sticky sessions,
   adding instances adds capacity, and losing one loses no conversation.
2. **A turn is one transaction.** It works on a private copy and at the end writes the state, the
   new transcript rows, the audit events and the queued effects together, or nothing.
3. **One turn at a time per conversation, held by a lease.** One atomic `UPDATE` takes the
   conversation for 20 s and a background thread renews it while the turn runs. A process that
   dies stops renewing, so its conversations are free again within 20 s without anyone cleaning
   up. A turn that lost its lease cannot write: its commit checks the owner and the version.
4. **Every message can be sent again.** The client attaches a `message_id`. If that message was
   already answered, the stored answer comes back (`"replayed": true`) and no turn runs. "The
   instance died", "the answer got lost" and "the request timed out" therefore all have the same
   remedy on the client: send it again.
5. **The outside world is reached through a queue.** An e-mail or a handoff to a human is a row
   in `outbox`, written in the turn's transaction and delivered afterwards by a dispatcher with
   retries, backoff and an idempotency key. A slow mail system cannot slow a turn down, a crash
   cannot lose an e-mail, and no e-mail goes out for a turn that was never committed.
6. **Every wait has a limit, and every refusal says when to come back.** Turn slots, the wait for
   a busy conversation, the store, each model call, the turn as a whole, the size of a
   conversation: each has a bound, and what exceeds it gets an error with `Retry-After` instead
   of a queue that grows.

### What happens when something fails

| What fails | What the system does | What the caller sees | Checked by |
|---|---|---|---|
| **An instance dies in the middle of a turn** | Nothing of the turn was written. Its lease lapses within `SOP_TURN_LEASE_SECONDS` (20 s). | The request fails; the retry (same `message_id`) waits for the lease if need be (`409 conversation_busy` + `Retry-After` meanwhile) and is answered by another instance. | `scripts/failover_test.py` (kills one of three instances mid-run); `test_store_leases.py` (a killed process's hold) |
| **An instance dies after the commit, before the answer arrives** | The turn is stored. | The retry gets the stored answer, marked `replayed`; the turn does not run twice. | `test_idempotent_messages.py` (the retry is answered by another server process on the same store); `failover_test.py` sends one message twice to two instances |
| **An instance is stopped for a deploy** (`SIGTERM`) | It stops accepting connections (so the load balancer moves on to the others), finishes the turns it has for up to `SOP_SHUTDOWN_GRACE_SECONDS` (30), stops its dispatcher and closes the store. A turn cut off by the grace period is not committed. | Nothing, or one retry. | `test_delivery_api.py` (a turn inside the grace period is committed, one cut off is not) |
| **The database is unreachable** | Every operation fails fast (`StoreUnavailable`); nothing is half-written. The same processes reconnect when it is back: no restart. | `503 server_busy` + `Retry-After`; `/readyz` says 503, so the load balancer stops sending traffic. | `test_store_postgres.py` (the server is stopped and started under a live store); the `ha` CI job stops and starts the database container |
| **The model API is slow or down** | One attempt is cut off after `SOP_MODEL_TIMEOUT_SECONDS` (60); no call starts later than `SOP_TURN_BUDGET_SECONDS` (120) into a turn. After 5 unanswered calls in a row the circuit breaker refuses calls at once for 15 s, then lets one probe through. One caller's bad key or rate limit never opens it. | Before the engine has decided: `503 model_unavailable` + `Retry-After`, nothing recorded. After it has decided: the fixed template wording for that step, so the decision is never lost. | `test_llm_resilience.py` |
| **The model API rate-limits the key** | The SDK retries with backoff; then the turn fails and nothing is recorded. | `429 rate_limited` with the provider's `Retry-After`. | `test_llm_resilience.py` |
| **The mail system or the agent desk is down** | Turns are unaffected. The effect stays in `outbox` and is retried with backoff (2 s … 5 min); after `SOP_DISPATCH_MAX_ATTEMPTS` (8) it is marked `dead` and shows in `/api/stats`. | The conversation continues; the delivery status in the UI stays "queued", then "failed". | `test_dispatch.py` (a sink that fails, hangs, recovers) |
| **An instance dies after sending an e-mail, before recording it** | The claim on the effect lapses (120 s) and another dispatcher sends it again, with the same idempotency key (the e-mail's `Message-ID`, the webhook's `Idempotency-Key`). Delivery is at least once; the receiver de-duplicates. | At worst the same e-mail twice. | `test_dispatch.py` (a lapsed claim is redelivered) |
| **More callers than capacity** | A turn waits for a slot for 2 s, then is refused. Memory and latency stay bounded. | `503 server_busy` + `Retry-After` of about one turn; the UI retries by itself. | `test_serving.py`; the load test with slow model calls |
| **The same message is sent twice, or twice at once** | The second one waits for the first, then gets its answer. | One answer, once. | `test_idempotent_messages.py` (two threads, all three stores) |
| **New code is rolled out next to old code** | A record written by newer code keeps the fields older code does not know and writes them back; a record in a newer format is refused, not guessed at. Schema creation runs under an advisory lock, so instances starting together do not collide. | Nothing. | `test_store.py`, `test_store_postgres.py` |
| **The data key is wrong or missing** | Encrypted rows cannot be read: the turn fails and releases its hold; nothing is overwritten. | An error; the data is intact for the right key. | `test_store_crypto.py` |

### Single points of failure

| Part | In this repository | Redundant? |
|---|---|---|
| Application instances | Identical, stateless processes; `docker-compose.ha.yml` runs three behind a load balancer | **Yes.** Killing one loses nothing (the `ha` CI job and `failover_test.py` do exactly that). |
| Dispatcher (e-mail, handoff) | A thread in every instance (or `python -m insurance_claims.dispatch` on its own) | **Yes.** They share the queue through row claims (`FOR UPDATE SKIP LOCKED`); a dead one's claims lapse. |
| Session store | One PostgreSQL database | **Not in this repository.** A single database container is what `docker-compose.ha.yml` starts, and it is that file's single point of failure. In production this is a managed PostgreSQL with a synchronous standby and automatic failover (RDS Multi-AZ, Cloud SQL HA, Patroni). What the application needs for that is here and tested: it survives the database disappearing and coming back without a restart, and a turn whose commit did not go through is simply retried. What is not tested here is a real failover between two database nodes. |
| Load balancer | A Caddy container in `docker-compose.ha.yml` | **Not in this repository**: in production the platform's load balancer, which has more than one node. It needs no state (no stickiness). |
| Model API | One provider, one region | **No.** Its outage is contained (timeouts, breaker, template replies after the decision) but verification of new callers stops, because reading a message needs the model. A second provider or region behind the same backend interface would be the remedy. |
| E-mail / human-agent desk | Simulated by default; SMTP and a webhook are implemented | Their outage only delays deliveries (the queue). |

### What is stored, and where

| Table | One row per | Written | Holds | Kept |
|---|---|---|---|---|
| `conversations` | conversation | rewritten each turn (small) | the working state as JSON: phase, verification progress, counters, the selected claim, what the caller asked for, the summary e-mail as composed, the handoff summary | until idle for `SOP_SESSION_TTL_SECONDS` (4 h) |
| `messages` | transcript entry | once | role, text, turn, `message_id` | with the conversation |
| `traces` | turn | once | what the harness decided and why (for the UI's trace panel) | with the conversation |
| `audit_events` | decision | once | *what happened*, never what was said: phase changes, verification checks (which field, matched or not, never the value), tool calls and refusals, consent, escalation, e-mail queued, guard blocks | `SOP_AUDIT_RETENTION_DAYS` (90) after the conversation is gone |
| `outbox` | e-mail or handoff to deliver | once, then status updates | kind, payload, status (`pending`/`sent`/`dead`), attempts | until delivered and the conversation is gone |

- A turn appends rows; it never rewrites the transcript. The old layout stored a whole
  conversation as one JSON value and rewrote all of it on every turn (268 KB per turn for a long
  one); now a turn writes only what it added.
- With `SOP_DATA_KEY` set, the state, the transcript, the traces and the queued payloads are
  encrypted at rest (AES-256-GCM, each value bound to its row). The audit trail is not: it holds
  no personal data by construction, and `test_audit.py` checks that over random conversations.
- Identity values are not kept after they have served: once a caller is verified, the stated
  values are dropped from the state, and wrong values are remembered only as keyed digests (to
  count a repeated wrong value once).
- `GET /api/sessions/{id}/audit` returns the audit events and the delivery status of the effects.

### How far it scales

- **Instances.** Capacity grows with instances until the next ceiling. One process runs 64 turns
  at once (`SOP_MAX_CONCURRENT_TURNS`); with live turns of 4–7 s that is 9–15 turns a second, or
  200–400 callers in conversation ([the arithmetic](#the-arithmetic-for-live-turns)).
- **The first ceiling is the model provider.** 10,000 callers in conversation at once send about
  400 messages a second, which is about 1,000 model requests a second. No instance count helps
  before the account's rate limit allows that; `SOP_MAX_CONCURRENT_TURNS` × instances should be
  set to what the account sustains, and the rest is refused cleanly.
- **The database comes well after that.** A turn costs the database two short transactions (take
  the lease and load; commit), plus one small lease renewal every few seconds while it waits for
  the model. Two instances on one PostgreSQL, all on one laptop, answered 120–130 turns a second
  with every conversation checked ([Load test](#load-test)): four to six times what their 128
  turn slots can use with a live model (20–30 turns a second). Every query is by conversation
  id, so the data can be partitioned by it when one database is no longer enough; only the
  outbox claims and the sweeps look across conversations.
- **Connections.** A turn holds a database connection only for its two transactions, not while
  it waits for the model, so 20 connections per process are enough for 64 slots. Instances ×
  `SOP_STORE_POOL_SIZE` must stay below the database's `max_connections` (a pooler such as
  PgBouncer beyond that).
- **What does not scale out here:** the per-client rate limit is per instance (a gateway's job),
  and `/api/stats` is per process.

### Built and tested here, and designed only

| | |
|---|---|
| **Built, and exercised by tests or CI** | Stateless instances on a shared store (SQLite on one machine, PostgreSQL across machines); leases with renewal; idempotent messages; the outbox with a dispatcher, retries and dead-lettering; SMTP and webhook transports against local sinks; model timeouts, turn budget and circuit breaker; `/readyz`; graceful shutdown; encryption at rest; the audit trail; an instance killed mid-run on SQLite and PostgreSQL; three containers behind a load balancer with one killed and the database restarted (CI). |
| **Designed, not built or not testable here** | Database failover between nodes; a second model provider; a gateway (TLS, authentication, global rate limits); metrics, tracing and alerts beyond `/api/stats`; key rotation for the data key; a real mail system and a real agent desk (the transports exist, the systems are simulated, as the brief's fixtures simulate consent). |

## Scaling and limits

### What one process does, and why

A turn spends almost all of its time waiting for the model: two to four blocking calls to the
Messages API, a few seconds in all. The server is built around that:

1. **Before anything is parsed**, a request body over 16 KB is refused (`413`), by its declared
   length or, without one, by counting the bytes as they arrive. A message is at most 4,000
   characters (`400` beyond).
2. **Optionally**, a per-client token bucket answers `429` (off by default, see below).
3. **A turn slot.** At most `SOP_MAX_CONCURRENT_TURNS` turns run at once. A turn waits for a slot
   without holding a thread, first come first served, for at most `SOP_TURN_SLOT_WAIT_SECONDS`;
   then it gets `503 server_busy` with a `Retry-After` of about one turn's time. Refusing quickly
   keeps latency and memory bounded when demand exceeds capacity, instead of building a queue
   whose requests all time out.
4. **A worker thread** from a pool of the same size, used for turns only, so turns can never take
   the threads that serve everything else. Health checks, config, `/api/stats`, the UI's files,
   reading a conversation and starting one need no turn slot: a server whose slots are all busy
   still answers them at once (`test_serving.py` checks this).
5. **The conversation itself.** One turn at a time per conversation, through the session store.
   A second message for a conversation that is mid-turn waits up to
   `SOP_CONVERSATION_WAIT_SECONDS`, then gets `409 conversation_busy` (`Retry-After: 1`).
   The turn works on a private copy of the state, which is written back only when the turn
   succeeds: a turn that fails or is refused leaves the conversation exactly as it was.
6. **The message cap.** The `SOP_MAX_TURNS_PER_CONVERSATION`-th caller message is not read; it
   is answered by a handoff to a human, through the same escalation path as the other handoffs
   (fixed wording, a handoff summary, no model call). Anything after that is refused with
   `409 conversation_closed`. This bounds the size and the model cost of any one conversation.

| Answer | Error code | When | `Retry-After` | The test UI |
|---|---|---|---|---|
| `400` | `bad_request` | Malformed body, empty or over-long message, text that is not valid Unicode | — | error row |
| `413` | `payload_too_large` | Body over 16 KB | — | error row |
| `429` | `rate_limited` | The client's budget is spent (only with the rate limit on) | until the next token | retries by itself (≤10 s) |
| `503` | `server_busy` | No turn slot free within the wait; or the session store does not answer; or the instance is shutting down | ≈ median turn time, or `1` | retries by itself |
| `503` | `model_unavailable` | The model service has not answered five calls in a row, so calls are refused at once for a while (circuit breaker) | until the next probe, ≤ 15 s | retries by itself (≤10 s) |
| `503` | `model_overloaded` | The model service says it is overloaded, after the SDK's own retries | the provider's, else `5` | retries by itself |
| `504` | `turn_timeout` | The time allowed for one turn ran out before the engine could decide | — | error row |
| `409` | `conversation_busy` | The same conversation is mid-turn beyond the wait | `1` | retries by itself |
| `409` | `conversation_closed` | The message cap was reached earlier | — | error row, no retry |
| `409` | `message_id_reused` | The `message_id` was already used for a different text in this conversation | — | error row, no retry |
| `404` | `session_not_found` | Never created, expired, evicted to make room, or the server restarted with the memory store | — | if nothing had been answered yet, starts a new conversation and sends the message there; otherwise keeps the transcript, offers a new conversation and carries the unsent text into it |

The conversation is looked up before the model is called. So a message to a conversation the
server no longer has is answered `404 session_not_found` whatever key came with it; whether a
key is valid is only known once the model service has been asked, and that answer is
`401 invalid_api_key`.

Every one uses the error envelope `{"error": {"code", "message"}}` and changes nothing. The UI
retries a busy answer up to three times, honouring `Retry-After`, while the typing indicator
stays up; only then does it show the error row with its Retry button.

**Sending a message again is always safe.** A message may carry a `message_id` (8 to 64 letters,
digits, `-` or `_`). If the conversation already holds a message with that id, the answer that
was stored for it is returned with `"replayed": true`: the engine does not run and nothing is
written. The check happens while the conversation is held, so a retry that arrives while the
first attempt is still running waits for it and then gets its answer. The UI gives every message
an id and reuses it for its automatic retries and for the Retry button, and so do the scripts. A
client that sends no id gets the old behaviour: a retry after a lost answer runs the turn again.

### The arithmetic for live turns

- **Turns per process.** A live turn takes typically 3–7 s (see above): the median was 4.4 s
  over the ten scripted scenarios, and 6.8 s for the demo conversation, whose answers are long
  and drawn from the document guidance ([measured below](#with-the-live-model)). With 64 slots
  one process therefore answers at most about 64 / 4.4 s ≈ 15 turns per second, or 64 / 6.8 s ≈ 9
  when every turn is a long one. A caller sends a message every 20–30 s or so (reading, typing,
  waiting), so that is in the order of 200–400 callers in conversation at once per process,
  depending on what they ask — provided the model API account allows 9–15 turns × 2–3 calls
  ≈ 25–45 requests a second and the tokens that go with them. In practice the account's rate
  limits are the first ceiling: set `SOP_MAX_CONCURRENT_TURNS` to what the account sustains, and
  the server refuses the rest cleanly instead of collecting the API's `429`s.
- **Threads.** 64 turn threads plus the default pool of 40 for everything else, per process.
  Turns are I/O-bound, so the GIL is not the limit; CPU per turn is a few milliseconds.
- **Memory.** The state of a conversation is kept as JSON: ~2 KB new, ~20 KB after a typical
  four-message call, ~160 KB at the 60-message cap, up to ~370 KB if every message is 4,000
  characters. 50,000 conversations of typical length are about 1 GB, so the memory store also
  keeps to `SOP_MAX_STORE_MB` (1,024 by default) and evicts the least recently used beyond it.
  Size both to the machine. `/api/stats` shows `state_bytes_held`.
- **Cost.** The message cap bounds the model calls of one conversation to about 60 × 3. Measured
  with the default models, the five-message demo conversation took 12 model calls and, as the
  turn log counts them, about 16,000 input and 3,400 output tokens.

### Settings

| Variable | Default | Effect |
|---|---|---|
| `SOP_MAX_CONCURRENT_TURNS` | `64` | Turn slots per process, and the size of the turn thread pool |
| `SOP_TURN_SLOT_WAIT_SECONDS` | `2` | How long a turn waits for a slot before `503` |
| `SOP_CONVERSATION_WAIT_SECONDS` | `5` | How long a message waits for its conversation's previous turn before `409` |
| `SOP_MAX_TURNS_PER_CONVERSATION` | `60` | Caller messages per conversation; the last one is answered by a handoff |
| `SOP_SESSION_STORE` | `memory` | `memory`; a SQLite file (`sqlite:///relative/path.db`, `sqlite:////absolute/path.db`, or a plain path); or `postgresql://user:password@host:5432/db` |
| `SOP_STORE_POOL_SIZE` | `20` | PostgreSQL: connections per process. A turn holds one only for its two short transactions, not while it waits for the model |
| `SOP_DATA_KEY` | — | SQLite and PostgreSQL: encryption at rest (AES-256-GCM) of state, transcript, traces and queued payloads; 32 random bytes as base64 or hex. A malformed key stops the start |
| `SOP_AUDIT_RETENTION_DAYS` | `90` | SQLite and PostgreSQL: how long audit events are kept after their conversation is gone |
| `SOP_MAX_SESSIONS` | `50000` | Conversations held; the least recently used is evicted beyond it (every store) |
| `SOP_MAX_STORE_MB` | `1024` | Memory store only: state held; the least recently used is evicted beyond it (`0` = no limit) |
| `SOP_SESSION_TTL_SECONDS` | `14400` | An idle conversation expires (checked on every read and turn) |
| `SOP_TURN_LEASE_SECONDS` | `20` | SQLite and PostgreSQL: a turn holds its conversation by a lease that a background thread renews about every third of it. A process that dies stops renewing, so what it held is free again within this time (at least 5) |
| `SOP_TURN_MAX_SECONDS` | `300` | A turn's lease is renewed for at most this long; kept above the longest possible turn (the next three settings), so a running turn never loses its hold |
| `SOP_MODEL_TIMEOUT_SECONDS`, `SOP_MODEL_MAX_RETRIES` | `60`, `2` | One attempt of one model call is cut off after the timeout; the SDK retries a failed attempt (no connection, 429, 5xx) this many times |
| `SOP_TURN_BUDGET_SECONDS` | `120` | No model call starts later than this into a turn (`504 turn_timeout`, or the template reply once the engine has decided) |
| `SOP_MODEL_BREAKER_FAILURES`, `SOP_MODEL_BREAKER_SECONDS` | `5`, `15` | Circuit breaker: after that many calls in a row that the model service did not answer, calls are refused at once (`503 model_unavailable`) for that long, then one probe is let through. `0` failures = off |
| `SOP_SHUTDOWN_GRACE_SECONDS` | `30` | On `SIGTERM` the instance stops accepting and lets the turns it has finish for this long; a turn cut off is not committed |
| `SOP_WORKERS` | `1` | Worker processes (`--workers N`); more than one is refused with the memory store |
| `SOP_RATE_LIMIT_PER_MINUTE` | `0` | Messages per client per minute, per instance; new conversations get a quarter of it (at least 1). `0` = off |
| `SOP_TRUST_PROXY` | `0` | `1`: the client is the last `X-Forwarded-For` hop (the address your own proxy saw), for the rate limit. Otherwise the socket peer |
| `SOP_MOCK_LATENCY_MS` | `0` | Offline double only: each simulated model call sleeps this long |

"Last use", for expiry and eviction, is the creation of a conversation or the start or commit of
one of its turns; reading it is not use. A conversation mid-turn is never evicted or expired.

#### Delivery settings

The summary e-mail and the handoff to a human are queued with the turn and delivered afterwards
(`insurance_claims/dispatch.py`). By default nothing leaves the machine.

| Variable | Default | Effect |
|---|---|---|
| `SOP_DISPATCH` | `on` | `on`: this process also delivers. `off`: another process does (`python -m insurance_claims.dispatch`) |
| `SOP_DISPATCH_POLL_SECONDS` | `1` | Pause between rounds that found nothing to deliver |
| `SOP_DISPATCH_MAX_ATTEMPTS` | `8` | A delivery that fails temporarily is retried with backoff (2 s, 4 s … 5 min) this many times, then marked failed |
| `SOP_EMAIL_TRANSPORT` | `demo` | `demo` (one log line, nothing sent) or `smtp` |
| `SOP_SMTP_URL`, `SOP_EMAIL_FROM` | — | For `smtp`: `smtp://user:password@host:587` (STARTTLS when offered; a password is never sent in clear) or `smtps://…:465`, and the sender |
| `SOP_EMAIL_REDIRECT_TO` | — | Every e-mail goes to this one address instead, with the intended recipient masked in a header. The fixture addresses are fictitious, so a deployment of the demo must not mail them |
| `SOP_HANDOFF_TRANSPORT` | `demo` | `demo` or `webhook` |
| `SOP_HANDOFF_WEBHOOK_URL`, `SOP_HANDOFF_WEBHOOK_TOKEN` | — | For `webhook`: where the handoff (reason, summary, conversation id) is POSTed as JSON, with an `Idempotency-Key` header and the token as a Bearer token |

A misconfigured transport stops the start with a message that names the setting and no secret.

The rate limiter is in memory, so each worker and each instance keeps its own buckets: it
protects one process from one noisy client, nothing more. A global limit, quotas per user and
authentication belong in a gateway in front of the instances.

### Several workers, several instances

Conversations in the memory store belong to one process, so `--workers N` (or `SOP_WORKERS`)
with N > 1 is refused at start until `SOP_SESSION_STORE` names a shared store:

| Store | Shared by | Survives a restart | For |
|---|---|---|---|
| `memory` | one process | no | the demo, tests |
| SQLite file | the workers and instances on one machine | yes | one machine |
| PostgreSQL | instances on any number of machines | yes | production |

```bash
SOP_SESSION_STORE=sqlite:///sessions.db python -m insurance_claims --workers 4   # ./sessions.db
# two instances, one store, any request to either:
SOP_SESSION_STORE=/data/sessions.db python -m insurance_claims --port 8001 &
SOP_SESSION_STORE=/data/sessions.db python -m insurance_claims --port 8002 &
# containers share it through a volume (the image's /data belongs to its user):
docker run -d -p 8001:8000 -v sop-sessions:/data -e SOP_SESSION_STORE=/data/sessions.db sop-agent
# any number of machines, one database:
docker run -d -p 8000:8000 -e SOP_SESSION_STORE=postgresql://sop:password@db.internal:5432/sop sop-agent
# three containers, a load balancer and a PostgreSQL container in one go:
docker compose -f docker-compose.ha.yml up --build --wait
```

All three stores implement one interface (`insurance_claims/store/base.py`) and pass one set of
tests. The two SQL stores share their logic and every statement (`store/sql.py`); the layout,
the leases and the queue are described in
[Scale and failure: the design](#scale-and-failure-the-design). What differs:

- **SQLite**: one machine (keep the file on a local disk; network filesystems do not lock
  reliably) and one writer at a time for all processes on the file. A turn's commit is a single
  statement, so the file's write lock is held as briefly as possible.
- **PostgreSQL**: a connection pool per process; the database's own clock decides leases and due
  times, so the instances' clocks need not agree; the password is never logged; an unreachable
  database fails operations fast (`503`) and the same processes carry on when it is back.

Each process has its own turn slots and counters, so N instances have N × `SOP_MAX_CONCURRENT_TURNS`
slots in all, and every one of them runs a dispatcher for the queued e-mails and handoffs.

### Watching it

- `GET /healthz` is liveness: the process answers. `GET /readyz` is readiness: its session store
  answers too (within about 2 s) and it is not shutting down; otherwise `503 not_ready`. A load
  balancer should route by `/readyz`.
- `GET /api/stats` reports, without any conversation content or identifiers: the store and the
  conversations it holds, the queue of e-mails and handoffs (pending, sent, failed), turn slots
  in use and waiting, counters since start (conversations created, turns answered, messages
  replayed, `503`, `409`, `429`, `413`, model errors, conversations closed at the cap), the
  median and p95 latency of the last 1,000 turns, the state of the model circuit breaker, what
  the dispatcher delivered, retried and gave up on, uptime and the process id. With a shared
  store the conversations and the queue are the shared totals; counters and latency are per
  process, and the response says so.
- `GET /api/sessions/{id}/audit` returns the conversation's audit events (what was decided,
  never what was said) and the delivery status of its e-mails and handoff. With a SQL store it
  still answers after the conversation itself has expired.

Each turn also writes one log line, without message text or identifiers (the conversation id is
cut to 8 characters, in the access log too):

```
INFO:sop.turn:turn 1eddf222 phase=VERIFY_ID->PROCESS_CASE status=200 latency=2ms model_calls=3
INFO:sop.turn:turn ac2c58f0 phase=?->? status=409 conversation_busy latency=5003ms
```

### Load test

`scripts/load_test.py` (standard library only) plays many simulated callers at once against one
or more running instances and checks every conversation, not just status codes: each turn moves
the turn counter by exactly one, each conversation ends where its script says (verified on the
right claim, email sent once or skipped, handed off), no reply names another policyholder's
claim, reading a conversation back gives what the last turn returned, and eight messages sent at
the same instant to one conversation are all answered, one at a time. Every message carries a
`message_id`, which is sent again with each retry of it. It honours `Retry-After` like a client
would and exits non-zero if any conversation went wrong. It refuses to run against
the live model; `scripts/live_check.py` is the small check for that
([below](#with-the-live-model)).

```bash
python -m insurance_claims --mock --port 8021 &
python scripts/load_test.py http://127.0.0.1:8021 --callers 400 --concurrency 100

# two instances sharing one store; each conversation alternates between them
SOP_SESSION_STORE=/tmp/sessions.db python -m insurance_claims --mock --port 8021 &
SOP_SESSION_STORE=/tmp/sessions.db python -m insurance_claims --mock --port 8022 &
python scripts/load_test.py http://127.0.0.1:8021 http://127.0.0.1:8022 --callers 400 --concurrency 100
# (the same with SOP_SESSION_STORE=postgresql://user:password@host:5432/db for PostgreSQL)

# make the offline double as slow as the model, so the turn slots fill and 503 + Retry-After show
SOP_MOCK_LATENCY_MS=1500 python -m insurance_claims --mock --port 8023 &
python scripts/load_test.py http://127.0.0.1:8023 --callers 200 --concurrency 150
```

The offline double answers in about a millisecond, so without `SOP_MOCK_LATENCY_MS` the test
measures the harness itself (HTTP, store, engine) and the load generator on the same machine;
with it, the turn slots and the waits behave as they would in front of the model.

**Measured** with the commands above on a laptop (Intel i5-1245U, Windows, Python 3.14), server,
database and load generator all on that one machine, offline double, on the version described
here (2026-11-23):

| Setup | Callers / at once | Conversations correct | Turns/s | Turn latency p50 / p95 | Asked to come back later |
|---|---|---|---|---|---|
| 1 process, memory store (2 runs) | 400 / 100 | 400 of 400, both runs | 186–212 | 207–227 ms / 244–355 ms | 0 |
| 2 processes, one SQLite file (2 runs) | 400 / 100 | 400 of 400, both runs | 197–201 | 402–410 ms / 687–791 ms | 0 |
| 2 processes, one PostgreSQL 16 database (2 runs) | 400 / 100 | 400 of 400, both runs | 120–129 | 426–432 ms / 821–876 ms | 0 |
| 1 process, `SOP_MOCK_LATENCY_MS=1500`, within the slots | 120 / 60 | 120 of 120 | 13 | 3.0 s / 4.5 s | 0 |
| 1 process, `SOP_MOCK_LATENCY_MS=1500`, over the slots | 200 / 150 | 200 of 200 | 17 | 4.6 s / 6.2 s | 312 × `503`, all answered on retry |

How to read it:

- **Latency is what the caller sees, queueing included.** In the first row 100 callers send at
  once to one process, and almost all of the 200 ms is waiting in line for it: on the server,
  `/api/stats` showed a median of 1.6–2.0 ms and a p95 of 5.0–6.2 ms per turn. With SQLite most
  of the wait is for the file's single writer (server-side medians of 280–350 ms at this load),
  with PostgreSQL for the database and the connection pool (80–105 ms). In the last two rows the
  latency is the simulated model (two or three calls of 1.5 s) plus, over the slots, the wait
  for one.
- **The first three rows are several times what a live model allows.** 64 turn slots with turns
  of 4–7 s use 9–15 turns a second per process. The store is not what limits this service; the
  model account is.
- **PostgreSQL costs more per turn than SQLite here**, with the database on the same laptop: a
  turn is about seven round trips to another process, against function calls into a local file.
  It is what lets the instances run on different machines. In these runs every instance also
  ran its dispatcher, which delivered the 400 summary e-mails (demo transport) from the same
  store while the test went on.
- **Absolute numbers move with the machine.** This laptop's speed drifts by a factor of about
  three with its temperature (the same test has given 116 and 400 turns/s on one day). Compare
  rows measured together, as these were.
- **The slots do what they are for.** Within them nothing is refused and the latency is the
  model's. Over them, throughput stays at what 64 slots give (64 ÷ ~4 s ≈ 16 turns/s), the
  excess is told when to come back, and every conversation still ends correctly.

**An instance is killed in the middle** (`scripts/failover_test.py`: three instances on one
store, 60 callers, each model call slowed to 350 ms so that turns are in flight, a turn lease of
6 s; one instance is killed outright after 3 s and started again):

| Store | Conversations correct | Messages answered exactly once | In flight on the killed instance | Longest wait for one answer | Summary e-mails sent, once each |
|---|---|---|---|---|---|
| SQLite | 60 of 60 | 300 of 300 | 19 turns | 9.7 s | 60 of 60 |
| PostgreSQL 16 | 60 of 60 | 300 of 300 | 20 turns | 9.7 s | 60 of 60 |

The callers whose turn died with the instance got a broken connection, sent the same message
again with the same id, were told `409 conversation_busy` until the dead instance's lease had
lapsed (6 s here, 20 s by default), and were then answered by another instance. Nothing was
answered twice and nothing was lost; afterwards all three instances, the restarted one included,
returned identical conversations.

Not measured again on this version: how the stores behave with tens of thousands of open
conversations. The previous version was measured at 33,000 (about 8 ms a turn, unchanged from an
empty store). For the SQL stores `test_store_split.py` checks instead, through SQLite's
statement trace, that creating, reading and answering run no statement that looks at all
conversations; expiry and eviction work in bounded batches in the background.

### With the live model

`scripts/load_test.py` stays away from the live model, so `scripts/live_check.py` is the small
version for it. N callers run the demo conversation at the same moment, four messages go to one
conversation at the same instant, and, when the instance was started with a small message cap,
one conversation is taken to the cap. The wording is the model's, so every conversation is
checked on its state (verified, the right claim, phases in order, one email, the turn counter).

```bash
python -m insurance_claims --port 8000 &                 # live model, key from the environment
python scripts/live_check.py http://127.0.0.1:8000       # 12 callers at once, about 65 live turns

# two workers on one SQLite file, and a cap small enough to reach
SOP_SESSION_STORE=/tmp/live.db SOP_MAX_TURNS_PER_CONVERSATION=7 python -m insurance_claims --workers 2 --port 8031 &
python scripts/live_check.py http://127.0.0.1:8031 --callers 8

# an instance killed under live turns: three live instances on one database, started by hand
python scripts/failover_test.py --urls http://127.0.0.1:8066,http://127.0.0.1:8067,http://127.0.0.1:8068 \
    --callers 8 --lease 6 --kill-after 4 --wait-margin 30 --give-up 240 --kill-cmd "kill -9 <pid of one of them>"
```

**Measured** on the same laptop with the default models (`claude-opus-5-5` for the replies,
`claude-haiku-5-5` for the structured calls), on 2026-11-23, on this version (after the storage
and delivery rework):

| Setup | Conversations at once | Conversations correct | Live turns | Took (one after another) | Turn time p50 / p95 | Model errors / `429` |
|---|---|---|---|---|---|---|
| 1 process, memory store | 12 | 12 of 12 | 60 | 35 s (392 s) | 6.8 s / 7.8 s | 0 / 0 |
| 3 instances on one PostgreSQL, one of them killed 4.6 s in | 8 | 8 of 8 | 40, each answered once | 46 s | 7.5 s; longest wait 18.6 s | 0 / 0 |

- **Twelve at once were no slower per turn than one caller alone**: the same five messages sent
  by a single caller took 7.8, 7.2, 10.2, 4.1 and 6.7 s. The time is the model's.
- **Four messages at the same instant to one conversation** were answered as turns 2, 3, 4 and 5,
  none lost, in about 31 s. A live turn takes longer than `SOP_CONVERSATION_WAIT_SECONDS`, so the
  waiting messages were told `409 conversation_busy` six times in all and were answered on retry.
- **An instance killed while it was waiting for the model.** Three instances shared one
  PostgreSQL with a turn lease of 6 s, shorter than a live turn, so every turn had its lease
  renewed while the model was working. All eight callers were in their first turn when one
  instance was killed; three of those turns were on it. Their callers got a broken connection,
  sent the same message again with the same id, were told `409 conversation_busy` until the
  dead instance's lease had lapsed, and were answered by another instance, 18.6 s after the
  first attempt at the longest. No message was answered twice, none was lost, the finished
  conversations read the same on both remaining instances, and all eight summary e-mails were
  queued once and sent.
- **With a cap of 7**, the seventh message was answered by the handoff to a human and the eighth
  with `409 conversation_closed`, which left the conversation as it was.
- **The 24 requirement checks** (`scripts/spec_check.py`, 22 live turns): 23 passed as written.
  The 24th, "a clarification question is answered without advancing", failed on its word list
  and not on the behaviour: the phase stayed `VERIFY_ID`, nothing was disclosed, and the reply
  said the verification is there so that "only you, and not someone pretending to be you" can
  see the claim, a phrasing the list did not have. The list was widened afterwards; the check
  was not run again.
- **The ten scripted scenarios** (`scripts/run_scenarios.py`; 38 turns, median 4.4 s, p95 7.6 s)
  each ended where intended (verified, handed off, or still waiting for consent) and every
  reply passed the output guard. After "Yes, please send it" the reply said the summary is "on
  its way", which is what the delivery queue makes true.
- **The model address failing, with the real SDK client.** No request reached the model service,
  so this cost nothing. With nothing listening at the address, five messages were answered
  `502 llm_error` after about 4.5 s each (two attempts per call); from the sixth on the answer
  was `503 model_unavailable` with `Retry-After: 15`, at once. The conversation was unchanged
  (turn 0, nothing kept) and `/readyz` stayed 200, since the instance itself was healthy. With
  an address that accepts the connection and never answers, and a timeout of 5 s, each message
  was answered `504` after 5.0 s. An outage of the real service (its own `5xx` or `529`
  answers) was not observed; those paths are tested with made-up responses in
  `test_llm_resilience.py`.

Not run again on this version: the same check with two worker processes on one SQLite file
(8 of 8 on 2026-11-22, on the previous version). The hosted demo runs this version too; with no
key supplied it answers offline, and the 24 requirement checks pass against it.

This checks that the server stays correct with several callers and the real model. It is not a
load test: it went to 12 conversations at once, not to the 64 slots, so the capacity in
[the arithmetic](#the-arithmetic-for-live-turns) is worked out from these turn times, not
measured at the limit.

### What is not here, and would be for production

- **A highly available database.** One PostgreSQL is a single point of failure. The application
  is ready for a managed PostgreSQL with a standby and automatic failover (it reconnects by
  itself and retries are safe), but no failover between database nodes was set up or tested.
- **A second model provider or region.** A model outage is contained, not bridged.
- **A load test with the live model up to the turn slots.** It would spend real credit and meet
  the account's rate limits first; the offline double with `SOP_MOCK_LATENCY_MS` stands in for it.
- **Asynchronous model calls.** Each running turn holds a thread while it waits for the model;
  the async client would let one process keep far more turns in flight, with the slots still as
  the bound. Today the answer to more load is more instances.
- **A gateway in front**: TLS, authentication, global rate limits and per-user quotas, and size
  limits at the edge. The limiter here is per instance, and a conversation id is all that is
  needed to read a conversation back.
- **Real receivers.** The SMTP and webhook transports were tested against local stand-ins only:
  no TLS handshake with a real mail server, no real agent desk. Delivery is at least once, so a
  receiver has to recognise a repeated idempotency key.
- **Key rotation** for `SOP_DATA_KEY`, and encryption of the columns kept readable for queries
  (phase, status, party id, delivery status and the audit trail).
- **Metrics and tracing** (Prometheus, OpenTelemetry) and alerts. `/api/stats` is per process
  and in memory.
- **Throttling failed verification per policyholder record** across conversations (see
  Assumptions).

## Tests

```bash
pip install -r requirements-dev.txt
pytest                                   # offline, no key needed
python scripts/run_scenarios.py          # scripted conversations against the live model
SOP_LLM_MODE=mock python scripts/run_scenarios.py demo frustrated   # same, offline
python scripts/live_check.py http://127.0.0.1:8000   # several callers at once, live instance
```

The suite runs without a model:

- `test_sop_flow.py` scripts the model's *reading* of each message and asserts what the harness
  does with it: the demo case, memory across phases, mismatch lockout, the refusal ladder, scope
  strikes, ambiguous claims, out-of-bounds resolver choices, tool scoping, email consent, the
  representative/consent paths, illegal transitions and the output guard.
- `test_invariants.py` treats the model's reading as untrusted input: 150 random 30-turn
  conversations with arbitrary schema-valid readings (contradictory flags, other people's
  identifiers and claim numbers, role flips, refusals), asserting after every turn that nothing
  behind the gate was touched before verification, verification is never re-pointed or undone,
  only owned claims are selected, email is sent only on a yes, nothing crashes, and the state
  survives the session store's JSON record unchanged. A companion test checks that those random
  runs really reach every phase and every escalation reason.
- `test_review_regressions.py` pins the defects an adversarial review of the harness found
  (representative-name swaps, an unguarded email summary, ambiguous "yes", refusal-ladder
  stalling, prompt-structure injection through the reading, tool-loop and truncation handling).
- `test_llm_wire.py` runs the Claude backend against a fake Messages API (mock HTTP transport)
  and checks what is sent — structured-output config, the tools offered per phase, that no record
  data or tools go out before verification, how tool results and thinking blocks are passed
  back — and how refusals and auth errors are handled.
- `test_api.py` covers the HTTP contract, including that a failed model call leaves the
  conversation untouched so the message can be retried.
- `test_store.py` holds the three session stores (memory, SQLite, PostgreSQL) to one contract:
  create, read, turn and commit, nothing written when a turn fails, not found, expiry, the cap
  and its eviction order, the bounded wait for a busy conversation, no lost update with many
  threads, two store objects and a second operating-system process on one store.
  `test_store_split.py` covers the layout (a turn appends rows and never rewrites the transcript,
  the hot path does no work that grows with the number of conversations, the audit trail outlives
  its conversation); `test_store_leases.py` the leases (renewal, a killed process's hold lapsing,
  a late commit refused); `test_store_effects.py` the queue (claimed by one dispatcher at a time,
  retried, set aside); `test_store_crypto.py` encryption at rest; `test_store_postgres.py` what is
  particular to PostgreSQL (the pool, the redacted password, the database stopped and started
  under a live store). The PostgreSQL tests need a server: `SOP_TEST_POSTGRES_URL`, or the
  embedded one that `requirements-dev.txt` installs on Windows; without either they are skipped.
- `test_audit.py` checks the audit trail: every decision leaves its event, events are only ever
  appended, and none of them contains anything a caller said or any identifier value, over the
  same random hostile conversations as `test_invariants.py`.
- `test_idempotent_messages.py` covers `message_id`: a retry is answered from what is stored, by
  another server process too; a retry racing the first attempt waits for it; the same id with
  another text is refused; on all three stores.
- `test_dispatch.py` runs the dispatcher against local receivers (`tests/sinks.py`: a small SMTP
  server and a webhook server that can fail, hang and recover): retry then success, failed for
  good after the attempts are used up, permanent failures, the redirect address, redelivery after
  a lapsed claim, two dispatchers on one store, a store outage. `test_delivery_api.py` covers
  what the API shows of it, `/readyz`, and a shutdown that lets turns finish within its grace
  period and commits nothing of a turn it had to cut off.
- `test_llm_resilience.py` covers the model failing: timeouts, the turn budget, the circuit
  breaker opening, probing and closing, a bad key or a rate limit never opening it, and the
  template reply when only the phrasing fails.
- `test_serving.py` fills the turn slots with held turns and checks that health, config, stats,
  static files, reads and new conversations still answer at once while an extra turn gets `503`
  with `Retry-After`; it also covers `409`, `413` (declared and streamed), `429`, the message
  cap, `/api/stats`, the turn log, the refusal of several workers without a shared store, and
  many conversations at once on threads with the memory and the SQLite store, compared with the
  same scripts run one at a time.
- `test_hostile_http.py` sends malformed and extreme input (invalid JSON, wrong types, missing
  fields, 4,000- and 4,001-character messages, control characters and NUL, emoji, right-to-left
  text, lone surrogates, 7,000-deep JSON, huge and malformed conversation ids, unknown methods
  and paths) with the memory and the SQLite store: never a 5xx, always the envelope or a normal
  reply, and a refused request changes nothing.

`scripts/run_scenarios.py` is the live check: it plays ten scripted conversations (demo case,
memory, frustration, refusal, mismatches, off-topic, representative, consent timeout, ambiguity,
human request) against the real model and prints each reply with the harness decisions behind it.
`scripts/live_check.py` does the same for several callers at once against a running instance
([With the live model](#with-the-live-model)).

`scripts/failover_test.py` is the check for [failure recovery](#what-happens-when-something-fails):
it starts three instances on one store, plays 60 conversations, kills one instance outright in the
middle and starts it again, and then checks every transcript (each message answered exactly once,
in order), the longest wait any caller had, that all instances show the same conversations, and
that every summary e-mail was sent once.

```bash
python scripts/failover_test.py                                              # a temporary SQLite file
python scripts/failover_test.py --store postgresql://user:password@host/db   # PostgreSQL
```

### Checking the Docker image

`.github/workflows/docker.yml` does this on a GitHub-hosted Linux runner on every push, with no
model key: it builds the image, checks what is in it, starts it in offline mode, waits for the
image's own `HEALTHCHECK`, confirms it runs as the non-root `appuser`, and plays the demo test
case through all four phases over the published port, then checks the brief's requirements one by
one (`scripts/spec_check.py`), and runs the load test against it. It then starts a second
container in model mode with no key (expects a clean `missing_api_key`), a third in `auto` mode
with no key (the hosted demo's configuration, which must answer offline and pass the same
requirement checks), two more that share a SQLite session store on a volume (one with two
workers) for a load test whose conversations alternate between them, checks that the image
refuses several workers with the in-memory store, and repeats the walk-through via
`docker compose`.

A second job runs the test suite inside `python:3.14-slim` next to a PostgreSQL container, so the
store tests cover all three stores there, and then the failover test on SQLite and on PostgreSQL.
A third job starts `docker-compose.ha.yml` (three instances behind a load balancer on
PostgreSQL), runs the requirement checks and the load test through the load balancer, kills one
container in the middle of a failover run and starts it again, and finally stops and starts the
database container to see the instances recover without a restart.

The same walk-through by hand, against any running instance:

```bash
docker run -d --name sop-test -p 8000:8000 -e SOP_LLM_MODE=mock sop-agent
python scripts/docker_smoke.py http://127.0.0.1:8000
python scripts/spec_check.py http://127.0.0.1:8000
docker rm -f sop-test
```

`scripts/spec_check.py` goes through the brief one requirement at a time against an offline
instance (`--mock`) and prints a PASS/FAIL line for each: no claim details before verification,
two fields are not enough, partial answers add up, alternate fields, refusals, the hint remembered
during `VERIFY_ID` and used afterwards, the demo case end to end, grounded answers, send and skip
for the summary email, off-topic questions and the human offer, the frustrated caller. It can also be
pointed at an instance running the live model (22 turns of API credit); the wording varies
there, so read a FAIL with `-v` before believing it.

## Project layout

```
insurance_claims/
  fixtures/            starter data (unchanged)
  sop.py               the SOP as data: phases, freedom, disclosure, tools, transitions, gates
  policy.py            deterministic engine: memory, guardrails, phase handlers, escalation
  identity.py          identifier normalisation and matching
  tools.py             bounded tools with per-phase / per-caller authorisation
  understanding.py     schemas for the structured model calls
  prompts.py           system prompts and control-block rendering
  llm.py               Claude backend (structured outputs, tool loop, refusal handling, timeouts,
                       turn budget, circuit breaker)
  mock_llm.py          offline test double
  replies.py           template wording for every directive
  guard.py             output checks (leak + grounding)
  agent.py             turn orchestrator
  state.py             session state, its audit events, its display-safe view and its JSON record
  store/               session stores behind one interface: memory, SQLite, PostgreSQL
    base.py              the interface: conversations, turns, audit trail, queue of effects
    sql.py               what the two SQL stores share: layout, statements, leases, sweeps
    sqlite.py postgres.py memory.py crypto.py
  dispatch.py          delivery of queued e-mails and handoffs: dispatcher, SMTP and webhook
  server.py            FastAPI app and API: turn slots, limits, idempotent messages, error
                       envelope, /readyz, /api/stats, the audit endpoint
  static/              the test UI (plain HTML/CSS/JS, no build step)
SUBMISSION.md                one-page cover note for reviewers
render.yaml                  Render Blueprint for a hosted copy of the demo
scripts/run_scenarios.py     scripted conversations against the live model (or offline)
scripts/docker_smoke.py      HTTP walk-through of the demo case against a running container
scripts/spec_check.py        the brief, requirement by requirement, against an offline instance
scripts/load_test.py         many simulated callers against running instances, each conversation checked
scripts/failover_test.py     kills one of three instances mid-run and checks every conversation
docker-compose.ha.yml        three instances behind a load balancer (deploy/Caddyfile) on PostgreSQL
scripts/live_check.py        several callers at once against an instance on the live model
.github/workflows/docker.yml image build + container checks; tests with PostgreSQL; failover; the three-instance setup
tests/
```

## Assumptions and choices worth knowing

- **Demo clock.** The fixtures are dated around early March 2026 (appeal deadlines on
  2026-03-18 and 2026-04-15), so the agent reasons with `SOP_TODAY=2026-03-02` by default;
  otherwise every deadline would already have passed and "January" would be ambiguous.
- **Representatives and consent** are inferred from `representatives.json` and
  `consent_scenarios.json`: a listed representative plus policyholder consent, on top of the
  same 3-of-5 rule. Consent is polled once per caller turn; `timeout` gives up after three checks.
- **Email** goes only to the address on file, shown masked. It is queued with the turn and
  delivered by the dispatcher; the UI shows it with its delivery status. By default the transport
  is the demo one, which sends nothing: the brief does not ask for a mail system, and its own
  fixtures simulate the consent service the same way. An SMTP transport is included for a real one.
- **Human transfer** works the same way: the conversation is closed, a handoff summary is
  produced and queued, and the demo transport delivers it nowhere. A webhook transport is
  included for a real agent desk.
- **Sessions** are in memory by default (lost on restart), capped in number, size and idle time.
  `SOP_SESSION_STORE` switches to a SQLite file (one machine) or a PostgreSQL database (any
  number of machines); see [Scale and failure: the design](#scale-and-failure-the-design).
- **Verification feedback.** The agent tells the caller which identifiers matched and which did
  not (by field name). That is what makes partial answers and corrections feel natural, and it
  is what a human representative does, but it does confirm a correct guess. Strikes are counted
  per conversation; a production deployment should also throttle failed attempts per record
  across conversations. Switching to generic "I couldn't verify that yet" feedback is a small
  change in `prompts._section` and `replies._core` if the stricter posture is preferred.
- **Side effects.** The e-mail and the handoff leave through the queue described above, so a
  retried turn cannot send twice and a crash cannot lose one. The consent request to the
  policyholder is still simulated inside the turn (from `consent_scenarios.json`); a real consent
  service would be called through the same queue.
- **Personal data.** Identifier values are kept out of the state view, the trace and the audit
  trail, and are dropped from the stored state once the caller is verified. The transcript
  necessarily contains whatever the caller typed; with a SQL store it can be encrypted at rest
  (`SOP_DATA_KEY`) and it expires with the conversation.
- The Test data drawer and `/api/fixtures` exist to make evaluation easy; turn them off with
  `SOP_EXPOSE_FIXTURES=0`.

## Packaging for submission

```bash
python scripts/make_zip.py        # writes sop-agent.zip without .venv, caches, .env or local tooling files
```
