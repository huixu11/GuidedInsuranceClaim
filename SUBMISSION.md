# Submission note — SOP-Guided Conversational Agent

An insurance claims support agent that talks naturally but cannot leave its standard operating
procedure: `VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`.

Everything in the brief is implemented, including the bonus part. This page explains how, in
plain words. `README.md` has the full design, configuration and test details.

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

## A two-minute demo

1. Click the suggested reply marked with an arrow (**Demo test case**) and press Enter.
2. Keep clicking the arrow reply: a document question, a wrap-up, then *send* for the email
   summary. Four messages, four phases. The **SOP state** pane on the right shows what the
   harness decided each turn; its **Trace** tab has the detail.
3. Start a new conversation and try the other suggestions: **Frustrated caller** before
   verifying, **Out of scope** three times, **Skip the summary**.

**Test data** in the top bar lists the fixture identities and claims, so you know what to type.

## What was checked

- **299 automated tests**, all offline. They include 150 random 30-turn conversations that feed
  the engine arbitrary, even hostile, readings and assert after every turn that nothing behind
  the gate was touched before verification.
- **24 requirement checks** (`scripts/spec_check.py`), one per line of the brief. They pass
  against the offline stand-in, against the live model, and against the hosted demo.
- **Ten scripted conversations against the live model** (`scripts/run_scenarios.py`).
- **CI on every push** (GitHub Actions): builds the Docker image, runs it, plays the demo case
  through it, and runs the test suite on the image's Python.

## Limits worth knowing

- Email sending, the human handoff and the policyholder-consent service are simulated; sessions
  are held in memory.
- The offline stand-in understands language crudely (regular expressions). It is there to show
  the control flow without a key; judge the conversation quality with the live model.
- The UI was checked in desktop Chrome and at phone width.
- `README.md` → *Assumptions and choices worth knowing* lists the rest, including the demo clock
  (the agent reasons as if today were 2026-03-02, to match the dates in the fixtures).
