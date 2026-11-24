"""The audit trail (`SessionState.events`): what it records, in what order, and what it never holds.

The trail has to answer later, without the transcript: was the caller verified, how (which
identifier fields matched) and as whom (party id), which claim records were opened, what was
sent, why and when the conversation went to a human, and what the guard blocked. It may be kept
much longer than the conversation, so it holds ids, field names, counts, booleans and fixed codes
only: no identifier values, no message text, no text the model or the caller produced, no amounts.

The checks defined here (`check_audit_after_turn`, `assert_audit_clean`) also run over the random
hostile conversations of test_invariants.py.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import random
import re
from datetime import date
from typing import Any, Callable, Iterable

import pytest

from insurance_claims import guard, identity, policy, tools
from insurance_claims.agent import ConversationClosed
from insurance_claims.llm import LLMRefusal
from insurance_claims.state import APPEND_ONLY_FIELDS, AUDIT_EVENTS, Phase, SessionState, Trace
from insurance_claims.tools import Toolbox, ToolError
from insurance_claims.understanding import INTENTS, ROLES

from .conftest import MARGARET, Conversation, ScriptedBackend

DEMO = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)
REP = dict(role="representative", rep_name="David Chen", relationship="son",
           full_name="Margaret Chen", dob="1985-03-15", phone="650-521-2836")
CLAIM_TOOLS = {"list_claims", "get_claim_details", "get_document_guidance", "send_summary_email"}


# ---------------------------------------------------------------------------
# What an event may hold
# ---------------------------------------------------------------------------
COUNT = "count"  # a count or a limit: a small non-negative int
FLAG = "flag"  # a boolean
PHASES = {p.value for p in Phase}
PII = set(identity.PII_FIELDS)


def audit_schema(repo: Any) -> dict[str, dict[str, Any]]:
    """Every event type, each key its data may have, and what the value may be: a set of allowed
    values, `[set]` for a list of them, COUNT or FLAG. Anything else is a test failure, so a new
    key or code has to be added here on purpose (and checked for being free text) first."""
    parties = {h.party_id for h in repo.policyholders}
    cases = {c["case_id"] for c in repo.claims}
    return {
        "conversation_started": {"consent_scenario": set(repo.consent_scenarios), "phase": PHASES},
        "tool_called": {
            "tool": set(tools.TOOL_NAMES) | {"unknown"},
            "actor": {"harness", "llm"},
            "case_id": cases | {None},
            "case_ids": [cases],
            "topic": set(tools.GUIDANCE_TOPICS),
            "party_id": parties,
            "refused": FLAG,
            "reason": set(tools.REFUSAL_REASONS),
        },
        "identity_checked": {
            "matched_fields": [PII],
            "mismatched_fields": [PII],
            "matched_count": COUNT,
            "policy_number_status": {identity.MATCHED, identity.MISMATCHED, identity.NOT_PROVIDED},
            "candidate_party_id": parties | {None},
        },
        "verification_strike": {"fields": [PII], "failed_attempts": COUNT, "max_failed_attempts": COUNT},
        "verification_refusal": {
            "refusal": {"identity_info", "consent"},
            "refusal_count": COUNT,
            "max_persuasion_attempts": COUNT,
        },
        "identity_verified": {
            "party_id": parties,
            "caller_role": set(ROLES),
            "matched_fields": [PII],
            "matched_count": COUNT,
            "required_matches": COUNT,
            "identifier_values_cleared": FLAG,
        },
        "role_change_ignored": {"kept_role": {"representative"}},
        "consent_requested": {"party_id": parties, "channel": {"text_to_phone_on_file"}},
        "consent_result": {"party_id": parties, "result": {"approved", "timed_out", "not_on_file"}, "polls": COUNT},
        "phase_changed": {
            "from": PHASES,
            "to": PHASES,
            "reason": {"identity_verified", "claim_selected", "caller_finished", "different_claim",
                       "follow_up", "claim_question"},
        },
        "claim_selected": {"case_id": cases, "path": set(INTENTS) - {"none"}},
        "claim_choice_rejected": {"case_id": cases | {None}, "reason": {"not_on_account"}},
        "out_of_scope": {"scope": {"out_of_scope", "mixed"}, "consecutive": COUNT, "total": COUNT, "limit": COUNT},
        "human_offered": {"reason": set(policy.HUMAN_OFFER_REASONS)},
        "escalated": {"reason": set(policy.ESCALATION_REASONS), "verified": FLAG, "phase": PHASES},
        "turn_limit_reached": {"limit": COUNT},
        "email_offered": {"remembered_preference": {"send", "skip", None}},
        "email_other_address_declined": {"reasks": COUNT, "max_reasks": COUNT},
        "email_decision": {"decision": {"send", "skip"}, "defaulted": FLAG},
        "email_queued": {
            "recipient": {"address_on_file"},
            "case_ids": [cases],
            "summary_source": {"drafted", "records"},
        },
        "guard_blocked": {
            "target": {"reply", "email_summary"},
            "rules": [set(guard.RULE_NAMES) - {guard.UNNAMED_RULE}],
            "outcome": {"template", "regenerated", "built_from_records"},
        },
        "model_refused": {"call": {"understand", "respond"}, "outcome": {"treated_as_out_of_scope", "template_reply"}},
        "conversation_closed": {
            "status": {"completed", "escalated"},
            "reason": {"ended_before_verification", "email_sent", "email_not_sent", "email_skipped"}
            | set(policy.ESCALATION_REASONS),
            "verified": FLAG,
            "phase": PHASES,
        },
        "conversation_reopened": {
            "reason": {"resumed_verification", "email_requested", "claim_question"},
            "phase": PHASES,
        },
    }


def _allowed(spec: Any, value: Any) -> bool:
    if spec == FLAG:
        return isinstance(value, bool)
    if spec == COUNT:
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1000
    if isinstance(spec, list):
        return isinstance(value, list) and all(_allowed(spec[0], v) for v in value)
    return (value is None or isinstance(value, str)) and value in spec


def vocabulary(repo: Any) -> set[str]:
    """Every word the trail may contain (lower case): event types, keys, codes and ids."""
    words = set(AUDIT_EVENTS)
    for data in audit_schema(repo).values():
        words |= set(data)
        for spec in data.values():
            members = spec[0] if isinstance(spec, list) else spec
            if isinstance(members, set):
                words |= {m for m in members if isinstance(m, str)}
    return {w.lower() for w in words}


def strings_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in (*strings_in(k), *strings_in(v))]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in strings_in(v)]
    return []


def assert_events_follow_schema(events: list[dict[str, Any]], repo: Any) -> None:
    schema = audit_schema(repo)
    for e in events:
        assert set(e) == {"seq", "turn", "at", "type", "data"}, e
        assert e["type"] in schema, e["type"]
        spec = schema[e["type"]]
        assert set(e["data"]) <= set(spec), (e["type"], sorted(set(e["data"]) - set(spec)))
        for key, value in e["data"].items():
            assert _allowed(spec[key], value), (e["type"], key, value)


def identifier_spellings(repo: Any) -> set[str]:
    """The identifier values of fixtures/policyholders.json and representatives.json, as stored
    and in the other spellings a caller (or the normalisers) would use."""
    out: set[str] = set()
    for h in repo.policyholders:
        for name in (h.name, *h.name_aliases):
            parts = name.split()
            out |= {name, identity.normalize("full_name", name), f"{parts[-1]}, {' '.join(parts[:-1])}", *parts}
        y, m, d = h.dob.split("-")
        month = date(int(y), int(m), int(d)).strftime("%B")
        out |= {h.dob, f"{m}/{d}/{y}", f"{int(m)}/{int(d)}/{y}", f"{d}.{m}.{y}", f"{y}/{m}/{d}", f"{y}{m}{d}",
                policy.fmt_date(h.dob), f"{int(d)} {month} {y}", f"{month[:3]} {int(d)}, {y}"}
        for phone in (h.phone, *h.phone_aliases):
            n = identity.normalize_phone(phone)
            out |= {phone, n, f"{n[:3]}-{n[3:6]}-{n[6:]}", f"({n[:3]}) {n[3:6]}-{n[6:]}", f"{n[:3]}.{n[3:6]}.{n[6:]}",
                    f"{n[:3]} {n[3:6]} {n[6:]}"}
        for email in (h.email, *h.email_aliases):
            out |= {email, email.split("@")[0]}
        out |= {h.id_last4, h.policy_number, identity.normalize_policy(h.policy_number), h.policy_number.split("-")[-1]}
    for r in repo.representatives:
        out |= {r["rep_name"], identity.normalize("full_name", r["rep_name"]), *r["rep_name"].split(),
                r["relationship"], r["buyer_name"]}
    return out


def reading_free_text(raw: dict[str, Any]) -> list[str]:
    """The free-text strings of one raw reading (the model's or a test's): identifier values,
    names, the summary, the off-topic topic, notes and the caller's words about a claim."""
    ident = raw.get("identity") or {}
    caller = raw.get("caller") or {}
    hints = raw.get("claim_hints") or {}
    texts = [raw.get("summary"), raw.get("out_of_scope_topic"), *ident.values(),
             caller.get("representative_name"), caller.get("relationship"),
             hints.get("case_id"), hints.get("time_reference"), hints.get("description"),
             *(raw.get("notes") or [])]
    return [str(t) for t in texts if t]


def assert_holds_none_of(obj: Any, texts: Iterable[str], skip: Iterable[str] = ()) -> None:
    """No text in `texts` appears in `obj` (JSON data), ignoring case: one shorter than four
    characters must not BE a key or value, a longer one must not be IN one, nor anywhere in the
    JSON. Texts in `skip` are not checked."""
    values = [s.lower() for s in strings_in(obj)]
    blob = json.dumps(obj, ensure_ascii=False).lower()
    skip = set(skip)
    for text in texts:
        t = str(text).strip().lower()
        if not t or t in skip:
            continue
        if len(t) < 4:
            assert t not in values, text
        else:
            assert t not in blob and not any(t in v for v in values), text


def assert_events_hold_none_of(events: list[dict[str, Any]], texts: Iterable[str], repo: Any) -> None:
    """No text in `texts` appears in the events' types and data (the wall-clock `at` is left
    out). A text that is itself one of the trail's fixed words or ids is skipped (a caller who
    typed "skip", a real claim number)."""
    assert_holds_none_of([[e["type"], e["data"]] for e in events], texts, vocabulary(repo))


def assert_audit_clean(state: SessionState, repo: Any, said: Iterable[str] = ()) -> None:
    """The trail holds only known keys and codes, and none of: the fixtures' identifier values,
    the messages (either side), the emails sent, the handoff summary, the free text in `said`."""
    assert_events_follow_schema(state.events, repo)
    texts = [*identifier_spellings(repo), *(m["text"] for m in state.messages), *strings_in(state.outbox), *said]
    if state.escalation:
        texts += state.escalation["handoff_summary"].splitlines()
    assert_events_hold_none_of(state.events, texts, repo)


def audit_snapshot(state: SessionState) -> dict[str, list[Any]]:
    """Deep copies of the append-only lists, for `check_audit_after_turn`."""
    return {name: copy.deepcopy(getattr(state, name)) for name in APPEND_ONLY_FIELDS}


def check_audit_after_turn(state: SessionState, kept: dict[str, list[Any]]) -> None:
    """After a turn: the append-only lists only grew, the trail is numbered, claim data was
    opened only after verification, and the trail agrees with the state's own counters."""
    for name, old in kept.items():
        new = getattr(state, name)
        assert new[: len(old)] == old, f"{name}: an entry from an earlier turn was changed, removed or moved"
        old.extend(copy.deepcopy(new[len(old):]))
    events = state.events
    assert [e["seq"] for e in events] == list(range(len(events)))
    types = [e["type"] for e in events]

    verified_at = types.index("identity_verified") if "identity_verified" in types else len(events)
    for e in events[:verified_at]:
        opened = e["type"] == "tool_called" and e["data"]["tool"] in CLAIM_TOOLS and not e["data"]["refused"]
        assert not opened, f"claim data opened before identity was verified: {e}"

    assert types.count("identity_verified") == (1 if state.verified else 0)
    if state.verified:  # the identifier values the caller typed are gone, and the trail says so
        assert state.identity_claims == {} and state.seen_mismatches == []
        assert [e["data"]["identifier_values_cleared"] for e in events if e["type"] == "identity_verified"] == [True]
    assert types.count("verification_strike") == state.failed_attempts
    assert types.count("verification_refusal") == state.refusal_count
    assert types.count("out_of_scope") == state.oos_total
    assert types.count("email_queued") == len(state.outbox)
    assert state.guard_blocks == sum(
        1 for e in events if e["type"] == "guard_blocked" and e["data"]["outcome"] != "regenerated"
    )
    escalated = [e["data"]["reason"] for e in events if e["type"] == "escalated"]
    assert escalated == ([state.escalation["reason"]] if state.status == "escalated" else [])
    ends = [e for e in events if e["type"] in ("conversation_closed", "conversation_reopened")]
    if state.status == "active":
        assert not ends or ends[-1]["type"] == "conversation_reopened"
    else:
        assert ends and ends[-1]["type"] == "conversation_closed" and ends[-1]["data"]["status"] == state.status
    # Every phase transition, including those that cascade within one turn.
    assert [(e["turn"], e["data"]["from"], e["data"]["to"]) for e in events if e["type"] == "phase_changed"] == [
        (h["turn"], h["from"], h["to"]) for h in state.phase_history[1:]
    ]
    # An email is queued only after an explicit "send" and a send that ran.
    for i, e in enumerate(events):
        if e["type"] == "email_queued":
            decision = next(x for x in reversed(events[:i]) if x["type"] == "email_decision")
            assert decision["data"]["decision"] == "send"
            assert any(x["type"] == "tool_called" and x["data"]["tool"] == "send_summary_email"
                       and not x["data"]["refused"] for x in events[decision["seq"]:i])


# ---------------------------------------------------------------------------
# Conversations whose every model-side text is kept, and which check the trail each turn
# ---------------------------------------------------------------------------
class RecordingBackend(ScriptedBackend):
    """The scripted backend, keeping every text the model side produced (readings, claim picks,
    summaries, replies), with switches for refusals and a queue of forced replies."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.reply_queue: list[str] | None = None
        self.refuse_next_reading = False
        self.refuse_next_reply = False

    def understand(self, state, user_text, settings):  # type: ignore[override]
        if self.refuse_next_reading:
            self.refuse_next_reading = False
            raise LLMRefusal("understand")
        raw, meta = super().understand(state, user_text, settings)
        self.texts += reading_free_text(raw)
        return raw, meta

    def resolve(self, state, claims, settings):  # type: ignore[override]
        raw, meta = super().resolve(state, claims, settings)
        self.texts += [str(raw.get("rationale") or ""), str(raw.get("case_id") or "")]
        return raw, meta

    def summarize(self, state, records, settings):  # type: ignore[override]
        raw, meta = super().summarize(state, records, settings)
        self.texts += strings_in(raw)
        return raw, meta

    def respond(self, state, directive, control_block, tool_specs, tool_executor, settings):  # type: ignore[override]
        if self.refuse_next_reply:
            self.refuse_next_reply = False
            raise LLMRefusal("respond")
        if self.reply_queue is not None:
            self.forced_reply = self.reply_queue.pop(0) if self.reply_queue else None
        result = super().respond(state, directive, control_block, tool_specs, tool_executor, settings)
        self.texts.append(result.text)
        return result


class AuditedConversation(Conversation):
    def __init__(self, agent: Any, backend: RecordingBackend, scenario: str = "default") -> None:
        super().__init__(agent, backend, scenario)
        self.kept = audit_snapshot(self.state)
        self.also_forbidden: list[str] = []  # text a test injected some other way

    def say(self, text: str = "(test)", **read: Any) -> str:
        reply = super().say(text, **read)
        check_audit_after_turn(self.state, self.kept)
        return reply

    @property
    def types(self) -> list[str]:
        return [e["type"] for e in self.state.events]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.state.events if e["type"] == kind]

    def tools_called(self, name: str) -> list[dict[str, Any]]:
        return [e["data"] for e in self.of("tool_called") if e["data"]["tool"] == name]


@pytest.fixture()
def make(agent):
    def new(scenario: str = "default", **settings_changes: Any) -> AuditedConversation:
        use = agent
        if settings_changes:
            use = type(agent)(agent.repo, dataclasses.replace(agent.settings, **settings_changes))
        return AuditedConversation(use, RecordingBackend(), scenario)

    return new


def in_order(sequence: list[str], wanted: list[str]) -> bool:
    """`wanted` occurs in `sequence` in this order (not necessarily next to each other)."""
    rest = iter(sequence)
    return all(any(item == w for item in rest) for w in wanted)


# ---------------------------------------------------------------------------
# The scripted flows (each one also runs the per-turn checks, and the privacy test below)
# ---------------------------------------------------------------------------
FLOWS: dict[str, Callable[[Callable[..., AuditedConversation]], AuditedConversation]] = {}


def flow(fn):
    FLOWS[fn.__name__] = fn
    return fn


def _to_email_offer(c: AuditedConversation) -> None:
    c.say("id", **MARGARET)
    c.say("denied healthcare", case_type="healthcare", status="denied")
    c.say("that's all", wants_to_end=True)
    assert c.action == policy.OFFER_EMAIL


@flow
def demo_and_email_sent(make):
    c = make()
    c.say(DEMO, role="policyholder", policy_number="POL-9921", case_type="healthcare",
          status="denied", time_reference="January", **MARGARET)
    c.say("How do I submit the missing documents?", intent="document_submission")
    c.say("That's all, thanks.", wants_to_end=True)
    c.say("Yes please.", yes_no="yes")
    return c


@flow
def demo_read_by_the_mock(make):
    c = make()  # the regex reading of the real sentences, so the reading's free text is the mock's
    for text in (DEMO, "How do I submit the missing documents?", "That's all, thanks.", "Yes please."):
        c.say(text)
    assert len(c.state.outbox) == 1
    return c


@flow
def strikes_lock_out(make):
    c = make()
    c.say("a", full_name="Margaret Chen", dob="1985-03-16")
    c.say("a again", dob="1985-03-16")  # the same wrong value: not a new strike
    c.say("b", id_last4="1111")
    c.say("c", phone="555-000-1234")
    return c


@flow
def refusal_ladder(make):
    c = make()
    for _ in range(3):
        c.say("no", refusal="identity_info", emotion="angry", emotion_intensity="strong")
    return c


@flow
def representative_consent_approved(make):
    c = make("default")
    c.say("rep", status="denied", **REP)
    c.say("she approved it")
    return c


@flow
def representative_consent_timed_out(make):
    c = make("timeout")
    c.say("rep", **REP)
    for _ in range(4):  # one more check after the request has timed out
        c.say("check again")
    c.say("yes, connect me", requests_human=True)
    return c


@flow
def representative_not_on_file(make):
    c = make()
    c.say("rep", **{**REP, "rep_name": "Bob Stranger"})
    c.say("still me")
    c.also_forbidden += ["Bob Stranger", "stranger bob"]
    return c


@flow
def role_change_ignored(make):
    c = make("timeout")
    c.say("rep", **REP)
    c.say("actually I am Margaret herself", role="policyholder")
    return c


@flow
def out_of_scope_escalation(make):
    c = make()
    for _ in range(4):
        c.say("What is RL?", scope="out_of_scope", out_of_scope_topic="reinforcement learning")
    return c


@flow
def email_skipped(make):
    c = make()
    _to_email_offer(c)
    c.say("no thanks", yes_no="no")
    return c


@flow
def email_never_answered(make):
    c = make()
    _to_email_offer(c)
    for _ in range(3):
        c.say("hmm")
    return c


@flow
def email_to_another_address_declined(make):
    c = make()
    _to_email_offer(c)
    c.say("Yes, but send it to my work address bob.smith@workmail.com instead.",
          yes_no="yes", email_summary_choice="send", email_to_other_address=True)
    c.say("Fine, the one on file then.", yes_no="yes")
    c.also_forbidden += ["bob.smith@workmail.com", "workmail"]
    return c


@flow
def skipped_then_sent_after_closing(make):
    c = make()
    _to_email_offer(c)
    c.say("skip it", email_summary_choice="skip")
    c.say("oh actually, yes please email me the summary", email_summary_choice="send", yes_no="yes")
    return c


@flow
def reopened_for_another_claim(make):
    c = make()
    _to_email_offer(c)
    c.say("skip", email_summary_choice="skip")
    c.say("thanks, bye")
    c.say("actually, what about my dental claim?", case_type="dental", wants_different_claim=True)
    return c


@flow
def ended_before_verification_then_resumed(make):
    c = make()
    c.say("bye", wants_to_end=True)
    c.say("wait, here are my details", **MARGARET)
    return c


@flow
def turn_limit(make):
    c = make(max_turns_per_conversation=3)
    for text in ("hi", "my name is", "still here"):
        c.say(text)
    with pytest.raises(ConversationClosed):
        c.say("one more")
    return c


@flow
def guard_blocks_replies(make):
    c = make()
    c.backend.forced_reply = (
        "Sure! Claim CL-2048 was denied because the review file did not include the pathology "
        "report and the treating provider office note."
    )
    c.say("why was my claim denied?", intent="denial_question")  # leaks before verification
    c.backend.forced_reply = None
    c.say("id", **MARGARET)
    c.say("denied healthcare", case_type="healthcare", status="denied")
    c.backend.reply_queue = ["You will be reimbursed $9,999.00 next week.", "The allowed maximum is $1,450.00."]
    c.say("how much will I get?", intent="payment_question")  # ungrounded, then a good second draft
    c.backend.reply_queue = None
    c.backend.forced_reply = "You will be reimbursed $9,999.00 next week."
    c.say("how much, really?", intent="payment_question")  # ungrounded twice: the template
    c.backend.forced_reply = None
    c.also_forbidden += ["$9,999.00", "9,999.00", "9999", "1450", "1,450"]
    return c


@flow
def drafted_summary_rejected(make):
    c = make()
    _to_email_offer(c)
    bad = {"subject": "Your claim", "discussed": ["Verified with SSN ending 4472 and DOB 1985-03-15."],
           "claim_status": "Claim CL-2048 is denied.",
           "next_steps": ["Expect a check for $9,999.00 next week (claim CL-3001)."]}
    c.backend.summarize = lambda *a, **k: (bad, {"call": "summarize"})
    c.say("yes", yes_no="yes")
    c.also_forbidden += [*strings_in(bad), "9,999", "9999"]
    return c


@flow
def model_refusals(make):
    c = make()
    c.backend.refuse_next_reading = True
    c.say("a message the model will not read")
    c.backend.refuse_next_reply = True
    c.say("my name is Margaret Chen", full_name="Margaret Chen")
    return c


@flow
def refused_tool_calls(make):
    c = make()
    repo, settings = c.agent.repo, c.agent.settings
    early = Toolbox(repo, c.state, Trace(turn=0, phase_before="VERIFY_ID"), settings)
    for call in (early.list_claims, lambda: early.get_claim_details("CL-2048"),
                 lambda: early.get_document_guidance("CL-2048", "other")):
        with pytest.raises(ToolError):
            call()
    c.say("id", **MARGARET)
    c.say("denied healthcare", case_type="healthcare", status="denied")
    later = Toolbox(repo, c.state, Trace(turn=99, phase_before="PROCESS_CASE"), settings)
    attempts = [
        ("get_document_guidance", {"case_id": "CL-3001", "topic": "submission_method", "document": ""}),
        ("get_document_guidance", {"case_id": "Margaret Chen 4472", "topic": "anything", "document": "her SSN"}),
        ("send_summary_email", {"subject": "x", "body": "y"}),
        ("drop all tables", {}),
    ]
    for name, args in attempts:
        with pytest.raises(ToolError):
            later.execute_llm_tool(name, args)
    c.also_forbidden += ["Margaret Chen 4472", "her SSN", "anything", "drop all tables"]
    return c


@flow
def resolver_choice_rejected(make):
    c = make()
    c.say("id", **MARGARET)
    picks = iter(["CL-3001", "Margaret Chen's 4472 claim"])
    c.backend.resolve = lambda state, claims, settings: (
        {"case_id": next(picks), "candidate_case_ids": [], "intent": "denial_question", "rationale": "x"}, {})
    c.say("the diagnosis report one", description="diagnosis report")
    c.say("that one", description="that one")
    c.also_forbidden += ["Margaret Chen's 4472 claim", "MARGARET CHEN'S 4472 CLAIM"]
    return c


@pytest.mark.parametrize("name", sorted(FLOWS))
def test_the_trail_of_every_scripted_flow_holds_no_personal_data_or_text(name, make, repo):
    c = FLOWS[name](make)
    check_audit_after_turn(c.state, c.kept)  # (some flows call tools between turns)
    assert c.state.events[0]["type"] == "conversation_started"
    assert_audit_clean(c.state, repo, [*c.backend.texts, *c.also_forbidden])


def test_the_schema_in_this_test_covers_exactly_the_declared_events(repo):
    assert {t: set(keys) for t, keys in audit_schema(repo).items()} == {
        t: set(keys) for t, keys in AUDIT_EVENTS.items()
    }


def test_the_scripted_flows_produce_every_event_type(make):
    seen = {t for fn in FLOWS.values() for t in fn(make).types}
    assert seen == set(AUDIT_EVENTS), sorted(set(AUDIT_EVENTS) - seen)


@pytest.mark.parametrize("bad", [
    {"type": "escalated",
     "data": {"reason": "caller_requested", "verified": False, "phase": "VERIFY_ID", "note": "hi"}},
    {"type": "out_of_scope", "data": {"scope": "out_of_scope", "consecutive": 1, "total": 1, "limit": "RL"}},
    {"type": "claim_selected", "data": {"case_id": "CL-9999", "path": "denial_question"}},
    {"type": "guard_blocked", "data": {"target": "reply", "rules": ["amount not in the claim data: $5"],
                                       "outcome": "template"}},
    {"type": "something_new", "data": {}},
])
def test_the_checks_themselves_reject_an_unknown_key_or_value(bad, repo):
    with pytest.raises(AssertionError):
        assert_events_follow_schema([{"seq": 0, "turn": 1, "at": 0.0, **bad}], repo)


@pytest.mark.parametrize("value", ["03/15/1985", "(650) 521-2836", "Chen, Margaret", "POL9921", "son", "matian"])
def test_the_checks_themselves_find_an_identifier_in_any_spelling(value, repo):
    shown = value if len(value) < 4 else f"x {value} y"  # a short value is checked as a whole value
    event = {"seq": 0, "turn": 1, "at": 0.0, "type": "human_offered", "data": {"reason": shown}}
    with pytest.raises(AssertionError):
        assert_events_hold_none_of([event], identifier_spellings(repo), repo)


def test_the_checks_themselves_catch_an_earlier_entry_being_changed(make):
    for name in APPEND_ONLY_FIELDS:
        c = make()
        c.say("id", **MARGARET)
        kept = audit_snapshot(c.state)
        getattr(c.state, name)[0]["turn"] = 42
        with pytest.raises(AssertionError):
            check_audit_after_turn(c.state, kept)


# ---------------------------------------------------------------------------
# What each flow records, in order
# ---------------------------------------------------------------------------
def test_demo_case_records_who_was_verified_how_before_any_claim_is_opened(make):
    c = demo_and_email_sent(make)
    events, types = c.state.events, c.types
    verified_at = types.index("identity_verified")
    first_claim_access = next(i for i, e in enumerate(events)
                              if e["type"] == "tool_called" and e["data"]["tool"] in CLAIM_TOOLS)
    assert verified_at < first_claim_access
    assert events[verified_at]["data"] == {
        "party_id": "P9", "caller_role": "policyholder", "matched_fields": ["full_name", "dob", "id_last4"],
        "matched_count": 3, "required_matches": 3, "identifier_values_cleared": True,
    }
    assert c.of("identity_checked")[0]["data"]["policy_number_status"] == "matched"
    # The first message cascades through three phases, each transition recorded.
    assert [(e["turn"], e["data"]["from"], e["data"]["to"], e["data"]["reason"]) for e in c.of("phase_changed")] == [
        (1, "VERIFY_ID", "RESOLVE_INTENT", "identity_verified"),
        (1, "RESOLVE_INTENT", "PROCESS_CASE", "claim_selected"),
        (3, "PROCESS_CASE", "POST_PROCESS", "caller_finished"),
    ]
    assert [e["data"] for e in c.of("claim_selected")] == [{"case_id": "CL-2048", "path": "denial_question"}]
    assert c.tools_called("list_claims")[0]["case_ids"] == ["CL-2102", "CL-2048", "CL-1899", "CL-2011"]
    assert {d["case_id"] for d in c.tools_called("get_claim_details")} == {"CL-2048"}
    assert c.tools_called("get_document_guidance") == [
        {"tool": "get_document_guidance", "actor": "llm", "case_id": "CL-2048", "topic": "submission_method",
         "refused": False}]
    # The email: offered, an explicit send, the send itself, then queued - to the address on file.
    assert in_order(types, ["email_offered", "email_decision", "tool_called", "email_queued", "conversation_closed"])
    assert c.of("email_decision")[0]["data"] == {"decision": "send", "defaulted": False}
    assert c.of("email_queued")[0]["data"] == {
        "recipient": "address_on_file", "case_ids": ["CL-2048"], "summary_source": "drafted"}
    assert c.of("conversation_closed")[-1]["data"] == {
        "status": "completed", "reason": "email_sent", "verified": True, "phase": "POST_PROCESS"}
    assert types.count("conversation_closed") == 1 and "escalated" not in types


def test_strikes_record_the_fields_and_the_running_count_and_the_lockout(make):
    c = strikes_lock_out(make)
    assert [(e["turn"], e["data"]["fields"], e["data"]["failed_attempts"]) for e in c.of("verification_strike")] == [
        (1, ["dob"], 1), (3, ["id_last4"], 2), (4, ["phone"], 3)]
    # Repeating a wrong value is neither a new check result nor a strike.
    assert not [e for e in c.state.events if e["turn"] == 2 and e["type"] != "tool_called"]
    assert [e["data"]["mismatched_fields"] for e in c.of("identity_checked")] == [
        ["dob"], ["dob", "id_last4"], ["dob", "phone", "id_last4"]]
    assert c.types[-2:] == ["escalated", "conversation_closed"]
    assert c.of("escalated")[0]["data"] == {"reason": "verification_failed", "verified": False, "phase": "VERIFY_ID"}
    assert "identity_verified" not in c.types and not any(c.tools_called(t) for t in CLAIM_TOOLS)


def test_refusal_ladder_records_each_refusal_the_offer_and_the_handoff(make):
    c = refusal_ladder(make)
    assert [(e["turn"], e["data"]["refusal_count"]) for e in c.of("verification_refusal")] == [(1, 1), (2, 2), (3, 3)]
    assert [(e["turn"], e["data"]["reason"]) for e in c.of("human_offered")] == [(2, "reluctant_to_verify")]
    assert [(e["turn"], e["data"]["reason"]) for e in c.of("escalated")] == [(3, "verification_refused")]
    assert c.of("conversation_closed")[-1]["data"]["status"] == "escalated"


def test_representative_with_consent_approved(make):
    c = representative_consent_approved(make)
    assert in_order(c.types, ["identity_checked", "consent_requested", "consent_result", "identity_verified",
                              "phase_changed", "claim_selected"])
    assert [(e["turn"], e["data"]) for e in c.of("consent_requested")] == [
        (1, {"party_id": "P9", "channel": "text_to_phone_on_file"})]
    assert [(e["turn"], e["data"]) for e in c.of("consent_result")] == [
        (2, {"party_id": "P9", "result": "approved", "polls": 1})]
    assert c.of("identity_verified")[0]["data"]["caller_role"] == "representative"
    assert c.tools_called("lookup_representative") == [
        {"tool": "lookup_representative", "actor": "harness", "party_id": "P9", "refused": False}]


def test_representative_with_consent_timed_out_records_the_result_once(make):
    c = representative_consent_timed_out(make)
    assert [(e["turn"], e["data"]) for e in c.of("consent_result")] == [
        (4, {"party_id": "P9", "result": "timed_out", "polls": 3})]
    assert len(c.tools_called("check_consent_status")) == 3  # no polling once it timed out
    assert [(e["turn"], e["data"]["reason"]) for e in c.of("human_offered")] == [
        (4, "consent_timed_out"), (5, "consent_timed_out")]  # each reply that offers a person
    assert [(e["turn"], e["data"]["reason"]) for e in c.of("escalated")] == [(6, "caller_requested")]
    assert "identity_verified" not in c.types and not any(c.tools_called(t) for t in CLAIM_TOOLS)


def test_representative_not_on_file_is_recorded_without_the_name(make):
    c = representative_not_on_file(make)
    assert [(e["turn"], e["data"]) for e in c.of("consent_result")] == [
        (1, {"party_id": "P9", "result": "not_on_file", "polls": 0})]
    assert "consent_requested" not in c.types
    assert [e["data"]["reason"] for e in c.of("human_offered")] == ["representative_not_on_file"] * 2


def test_a_representative_claiming_to_be_the_policyholder_is_recorded(make):
    c = role_change_ignored(make)
    assert [(e["turn"], e["data"]) for e in c.of("role_change_ignored")] == [(2, {"kept_role": "representative"})]


def test_out_of_scope_records_counts_the_offer_and_the_handoff(make):
    c = out_of_scope_escalation(make)
    assert [(e["data"]["consecutive"], e["data"]["total"]) for e in c.of("out_of_scope")] == [
        (1, 1), (2, 2), (3, 3), (4, 4)]
    assert [(e["turn"], e["data"]["reason"]) for e in c.of("human_offered")] == [(3, "repeated_out_of_scope")]
    assert in_order(c.types, ["out_of_scope", "escalated", "conversation_closed"])
    assert c.of("escalated")[0]["data"]["reason"] == "repeated_out_of_scope"


def test_email_skipped_sends_nothing_and_says_so(make):
    c = email_skipped(make)
    assert in_order(c.types, ["email_offered", "email_decision", "conversation_closed"])
    assert c.of("email_decision")[0]["data"] == {"decision": "skip", "defaulted": False}
    assert c.of("conversation_closed")[-1]["data"]["reason"] == "email_skipped"
    assert "email_queued" not in c.types and not c.tools_called("send_summary_email")


def test_no_answer_to_the_offer_is_recorded_as_a_default_skip(make):
    c = email_never_answered(make)
    assert c.of("email_decision")[0]["data"] == {"decision": "skip", "defaulted": True}
    assert len(c.of("email_offered")) == 1  # asked again, but offered once


def test_a_request_for_another_address_is_recorded_without_the_address(make):
    c = email_to_another_address_declined(make)
    assert [e["data"] for e in c.of("email_other_address_declined")] == [{"reasks": 1, "max_reasks": 3}]
    assert in_order(c.types, ["email_other_address_declined", "email_decision", "email_queued"])
    assert c.of("email_queued")[0]["data"]["recipient"] == "address_on_file"


def test_email_queued_only_after_the_decision_to_send(make):
    c = skipped_then_sent_after_closing(make)
    assert [e["data"]["decision"] for e in c.of("email_decision")] == ["skip", "send"]
    assert in_order(c.types, ["email_decision", "conversation_closed", "conversation_reopened", "email_decision",
                              "email_queued", "conversation_closed"])
    assert c.of("conversation_reopened")[0]["data"] == {"reason": "email_requested", "phase": "POST_PROCESS"}
    last_decision = len(c.types) - 1 - c.types[::-1].index("email_decision")
    assert c.types.index("email_queued") > last_decision


def test_a_closed_conversation_reopened_for_another_claim(make):
    c = reopened_for_another_claim(make)
    assert in_order(c.types, ["conversation_closed", "conversation_reopened", "phase_changed", "claim_selected",
                              "phase_changed"])
    assert c.of("claim_selected")[-1]["data"]["case_id"] == "CL-1899"
    assert c.of("phase_changed")[-2]["data"] == {"from": "POST_PROCESS", "to": "RESOLVE_INTENT",
                                                 "reason": "different_claim"}


def test_ending_before_verification_and_coming_back(make):
    c = ended_before_verification_then_resumed(make)
    assert c.of("conversation_closed")[0]["data"] == {
        "status": "completed", "reason": "ended_before_verification", "verified": False, "phase": "VERIFY_ID"}
    assert in_order(c.types, ["conversation_closed", "conversation_reopened", "identity_verified"])
    assert c.of("conversation_reopened")[0]["data"]["reason"] == "resumed_verification"


def test_turn_limit_is_recorded_as_a_handoff(make):
    c = turn_limit(make)
    assert c.types[-3:] == ["turn_limit_reached", "escalated", "conversation_closed"]
    assert c.of("turn_limit_reached")[0]["data"] == {"limit": 3} and c.of("turn_limit_reached")[0]["turn"] == 3
    assert c.of("escalated")[0]["data"]["reason"] == "turn_limit"
    check_audit_after_turn(c.state, c.kept)  # the refused fourth message recorded nothing


def test_guard_blocks_are_recorded_by_rule_name_and_outcome(make):
    c = guard_blocks_replies(make)
    blocked = [e["data"] for e in c.of("guard_blocked")]
    assert [b["outcome"] for b in blocked] == ["template", "regenerated", "template"]
    assert "claim_data_before_verification.case_id" in blocked[0]["rules"]
    assert blocked[1]["rules"] == blocked[2]["rules"] == ["ungrounded_amount"]
    assert all(b["target"] == "reply" for b in blocked)
    assert c.state.guard_blocks == 2  # a regenerated reply that passed is not counted as a block


def test_a_rejected_email_draft_is_recorded_and_the_email_says_it_came_from_the_records(make):
    c = drafted_summary_rejected(make)
    blocked = c.of("guard_blocked")[0]["data"]
    assert blocked["target"] == "email_summary" and blocked["outcome"] == "built_from_records"
    assert {"own_identifier.id_last4", "own_identifier.dob", "ungrounded_amount"} <= set(blocked["rules"])
    assert c.of("email_queued")[0]["data"]["summary_source"] == "records"
    assert "CL-3001" not in json.dumps([[e["type"], e["data"]] for e in c.state.events])


def test_model_refusals_are_recorded(make):
    c = model_refusals(make)
    assert [e["data"] for e in c.of("model_refused")] == [
        {"call": "understand", "outcome": "treated_as_out_of_scope"},
        {"call": "respond", "outcome": "template_reply"}]
    assert in_order(c.types, ["model_refused", "out_of_scope"])


def test_refused_tool_calls_record_the_reason_and_never_the_requested_text(make):
    c = refused_tool_calls(make)
    refused = [e["data"] for e in c.of("tool_called") if e["data"]["refused"]]
    assert refused == [
        {"tool": "list_claims", "actor": "harness", "refused": True, "reason": "not_allowed_in_phase"},
        {"tool": "get_claim_details", "actor": "harness", "case_id": "CL-2048", "refused": True,
         "reason": "not_allowed_in_phase"},
        {"tool": "get_document_guidance", "actor": "llm", "case_id": "CL-2048", "topic": "other", "refused": True,
         "reason": "not_allowed_in_phase"},
        {"tool": "get_document_guidance", "actor": "llm", "case_id": "CL-3001", "topic": "submission_method",
         "refused": True, "reason": "claim_of_another_party"},
        {"tool": "get_document_guidance", "actor": "llm", "case_id": None, "topic": "other", "refused": True,
         "reason": "claim_not_found"},
        {"tool": "send_summary_email", "actor": "llm", "refused": True, "reason": "unknown_tool"},
        {"tool": "unknown", "actor": "llm", "refused": True, "reason": "unknown_tool"},
    ]


def test_an_email_that_could_not_be_sent_is_recorded_as_not_sent(make, repo, monkeypatch):
    c = make()
    _to_email_offer(c)
    toolbox = Toolbox(repo, c.state, Trace(turn=99, phase_before="POST_PROCESS"), c.agent.settings)
    with pytest.raises(ToolError):
        toolbox.send_summary_email("subject", "body")  # no "yes" yet
    monkeypatch.setitem(repo._by_party, "P9", dataclasses.replace(repo.policyholder("P9"), email=""))
    c.say("yes please", yes_no="yes")
    assert c.tools_called("send_summary_email") == [
        {"tool": "send_summary_email", "actor": "harness", "refused": True, "reason": "no_consent"},
        {"tool": "send_summary_email", "actor": "harness", "refused": True, "reason": "no_address_on_file"}]
    assert in_order(c.types, ["email_decision", "tool_called", "conversation_closed"])
    assert c.of("conversation_closed")[-1]["data"]["reason"] == "email_not_sent"
    assert "email_queued" not in c.types and c.state.outbox == []
    assert_audit_clean(c.state, repo, c.backend.texts)


def test_a_claim_the_resolver_may_not_pick_is_recorded_by_id_only(make):
    c = resolver_choice_rejected(make)
    assert [e["data"] for e in c.of("claim_choice_rejected")] == [
        {"case_id": "CL-3001", "reason": "not_on_account"}, {"case_id": None, "reason": "not_on_account"}]
    assert "claim_selected" not in c.types


def test_every_guard_violation_has_a_fixed_rule_name(verified, repo):
    """The rule names are read off the violation texts: if the wording changes, this fails
    instead of the trail filling up with 'unnamed_rule'."""
    s = verified.state
    other = repo.claim("CL-3001")
    texts = [
        "margaret@email.com 650-521-2836 1985-03-15 SSN ending 4472",  # own, never echoed in an email
        "ava.lopez@email.com 650-388-2920 1990-08-21 SSN ending 9180 POL-1044",  # someone else's
        f"Claim CL-3001 was denied because {other['denial_reason']}",
    ]
    violations = [v for t in texts for v in guard.leak_violations(t, s, repo, allow_echo=False)]
    unverified = verified.agent.new_session()
    violations += guard.leak_violations(f"CL-2048: {repo.claim('CL-2048')['denial_reason']}", unverified, repo)
    violations += guard.premature_verification_claims("Great, you're verified.")
    violations += guard.grounding_violations("You get $9,999.00 for CL-9999.", s, [], set())
    names = guard.rule_names(violations)
    assert guard.UNNAMED_RULE not in names and set(names) <= guard.RULE_NAMES
    assert {"own_identifier.email", "own_identifier.phone", "own_identifier.dob", "own_identifier.id_last4",
            "other_person_identifier.email", "other_person_identifier.phone", "other_person_identifier.dob",
            "other_person_identifier.id_last4", "other_person_identifier.policy_number",
            "other_person_claim_data.case_id", "other_person_claim_data.denial_reason",
            "claim_data_before_verification.case_id", "claim_data_before_verification.denial_reason",
            "premature_verification", "ungrounded_amount", "ungrounded_claim_number"} <= set(names)
    assert guard.rule_names(["something new: 123"]) == [guard.UNNAMED_RULE]


# ---------------------------------------------------------------------------
# Wrong identifier values are kept only as keyed digests (seen_mismatches)
# ---------------------------------------------------------------------------
WRONG = {"dob": "March 16, 1985", "email": "Jordan.Example@Nowhere.test", "phone": "(555) 000-1234",
         "id_last4": "8213"}


def _record_without_clock(state: SessionState) -> dict[str, Any]:
    record = json.loads(json.dumps(state.to_record()))
    record["events"] = [{k: v for k, v in e.items() if k != "at"} for e in record["events"]]
    for t in record["traces"]:
        t.pop("latency_ms", None)
    return record


def test_wrong_identifier_values_are_kept_only_as_keyed_digests(make):
    c = make()
    c.state.session_id = "audit-test-session"  # fixed, so the digests are too
    c.say("name + dob", full_name="Margaret Chen", dob=WRONG["dob"])
    c.say("dob again, written differently", dob="03/16/1985")  # the same wrong value
    assert c.state.failed_attempts == 1
    c.say("email + phone", email=WRONG["email"], phone=WRONG["phone"])
    assert c.state.failed_attempts == 2
    c.say("ssn", id_last4=WRONG["id_last4"])
    assert c.state.failed_attempts == 3 and c.state.status == "escalated"

    seen = c.state.seen_mismatches
    assert [k.split(":")[0] for k in seen] == ["dob", "phone", "email", "id_last4"]
    assert all(re.fullmatch(r"[a-z_0-9]+:[0-9a-f]{16}", k) for k in seen)
    assert seen[0] == policy._mismatch_key("audit-test-session", "dob=1985-03-16")
    # The wrong values, as typed and as normalised, are in the record only where the caller's
    # own words are: the transcript and what they supplied for verification.
    record = _record_without_clock(c.state)
    assert {"dob", "email", "phone", "id_last4"} <= set(record["identity_claims"])
    rest = {k: v for k, v in record.items() if k not in ("messages", "identity_claims")}
    blob = json.dumps(rest, ensure_ascii=False).lower()
    for value in (*WRONG.values(), "1985-03-16", "03/16/1985", "jordan.example@nowhere.test", "5550001234",
                  "555-000-1234", "dob=", "email=", "phone=", "id_last4="):
        assert value.lower() not in blob, value


def test_a_wrong_value_kept_in_clear_by_an_earlier_version_is_still_recognised(agent):
    c = Conversation(agent, ScriptedBackend())
    c.say("name", full_name="Margaret Chen")
    c.state.seen_mismatches = ["dob=1985-03-16"]  # as a record written before the digests holds it
    c.state.failed_attempts = 1
    c.say("dob", dob="1985-03-16")
    assert c.state.failed_attempts == 1  # the same wrong value: still not a new strike
    assert c.state.seen_mismatches == [policy._mismatch_key(c.state.session_id, "dob=1985-03-16")]
    c.say("dob", dob="1985-03-17")
    assert c.state.failed_attempts == 2  # a different one is


IDENTITY_CHOICES = {
    "full_name": ["Margaret Chen", "Chen, Margaret", "Maggie Chen", "Jordan Example", "Ava Lopez"],
    "dob": ["1985-03-15", "1985-03-16", "03/16/1985", "March 16, 1985", "1970-01-01"],
    "phone": ["650-521-2836", "555-000-1234", "(555) 000-1234", "650-388-2920"],
    "email": ["margaret@email.com", "nobody@example.org", "NOBODY@example.org", "x@y.test"],
    "id_last4": ["4472", "1111", "123-45-1111", "0000"],
}


@pytest.mark.parametrize("seed", range(40))
def test_strikes_count_exactly_as_when_the_wrong_values_were_kept_in_clear(seed, agent, monkeypatch):
    def play() -> list[tuple[Any, ...]]:
        rng = random.Random(seed)
        c = Conversation(agent, ScriptedBackend())
        seen = []
        for _ in range(8):
            read = {f: rng.choice(v) for f, v in IDENTITY_CHOICES.items() if rng.random() < 0.35}
            seen.append((c.say("id", **read), c.state.failed_attempts, c.state.status, c.state.verified))
        return seen

    with_digests = play()
    monkeypatch.setattr(policy, "_mismatch_key", lambda session_id, plain: plain)  # the former key
    assert play() == with_digests


# ---------------------------------------------------------------------------
# The identifier values the caller typed are dropped once identity is verified
# ---------------------------------------------------------------------------
# Where the record keeps free text by design, as before: the transcript, the turn traces (the
# reading's summary and notes, tool output), what the model was shown, the emails sent, the
# caller's words about a claim, the representative's name, and the handoff summary.
FREE_TEXT_FIELDS = ("messages", "traces", "grounding", "outbox", "hints", "notes", "rep_name",
                    "rep_relationship", "consent_rep", "consent_ledger")

AFTER_VERIFICATION = dict(  # a verified caller states a role and identifiers again: none of it is used
    role="policyholder", full_name="Ava Lopez", dob="1990-08-21", phone="555-000-1234",
    email="someone.else@example.org", id_last4="9180", policy_number="POL-1044",
)

SCRIPTS: dict[str, tuple[str, list[tuple[str, dict[str, Any]]]]] = {
    "policyholder_after_a_strike": ("default", [
        ("I'm Margaret Chen, born March 16, 1985", dict(full_name="Margaret Chen", dob="March 16, 1985")),
        ("sorry: 1985-03-15, SSN 4472, my denied claim",
         dict(dob="1985-03-15", id_last4="4472", case_type="healthcare", status="denied")),
        ("my phone and email, and I'm Ava Lopez too", AFTER_VERIFICATION),
        ("How do I submit the missing documents?", dict(intent="document_submission")),
        ("That's all.", dict(wants_to_end=True)),
        ("Yes please.", dict(yes_no="yes")),
    ]),
    "handed_off_after_verification": ("default", [
        ("id", dict(MARGARET)),
        ("and here is everything again", AFTER_VERIFICATION),
        ("let me talk to a person", dict(requests_human=True)),
    ]),
    "representative_consent_approved": ("default", [
        ("rep", dict(status="denied", **REP)),
        ("she approved it", {}),
        ("I'm Margaret actually, here are her details again", AFTER_VERIFICATION),
        ("why was it denied?", dict(intent="denial_question")),
        ("that's all", dict(wants_to_end=True)),
        ("no thanks", dict(yes_no="no")),
        ("oh wait, what about the dental claim?", dict(case_type="dental", wants_different_claim=True)),
    ]),
    "representative_consent_timed_out": ("timeout", [
        ("rep", REP), *[("check again", {})] * 4, ("connect me", dict(requests_human=True)),
    ]),
    "representative_not_on_file": ("default", [
        ("rep", {**REP, "rep_name": "Bob Stranger"}), ("still me", {}),
        ("her email too", dict(email="margaret@email.com")),
    ]),
    "representative_role_change": ("default", [
        ("rep", REP),
        ("actually I am Margaret herself", dict(role="policyholder")),
        ("and again", AFTER_VERIFICATION),
        ("what's the status of the auto claim?", dict(case_type="auto", intent="status_inquiry")),
    ]),
}
NEVER_VERIFIED = ("representative_consent_timed_out", "representative_not_on_file")


def _play(agent: Any, scenario: str, script: list[tuple[str, dict[str, Any]]], checked: bool = True) -> Any:
    c = (AuditedConversation if checked else Conversation)(agent, RecordingBackend(), scenario)
    c.state.session_id = "audit-test-session"  # fixed, so the strike digests are too
    for text, read in script:
        c.say(text, **read)
    return c


def _observable(c: Any) -> dict[str, Any]:
    """Everything but the typed values: the display view and the whole record (replies, traces,
    phase history, counters, the trail), without the wall clock."""
    record = _record_without_clock(c.state)
    del record["identity_claims"], record["seen_mismatches"]
    return {"view": c.state.to_view(c.agent.settings), "record": record}


def _outside_free_text(state: SessionState) -> dict[str, Any]:
    record = _record_without_clock(state)
    for name in FREE_TEXT_FIELDS:
        del record[name]
    if record["escalation"]:
        del record["escalation"]["handoff_summary"]
    return record


def _typed_values(script: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [v for _, read in script for f in identity.ALL_ID_FIELDS if read.get(f)
            for v in (read[f], identity.normalize(f, read[f]) or "")]


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_dropping_the_typed_values_changes_nothing_else(name, agent, monkeypatch):
    """Including for a verified caller who states identifiers (or another role) again: nothing
    is stored and every reply, the view, the traces, the counters and the trail are as before."""
    scenario, script = SCRIPTS[name]
    dropping = _play(agent, scenario, script)
    monkeypatch.setattr(policy, "_forget_identifier_values", lambda s: None)  # as before this change
    keeping = _play(agent, scenario, script, checked=False)
    assert _observable(dropping) == _observable(keeping)
    if name in NEVER_VERIFIED:  # still verifying, or handed off before it: nothing is dropped
        assert not keeping.state.verified
        assert dropping.state.identity_claims == keeping.state.identity_claims != {}
        assert dropping.state.seen_mismatches == keeping.state.seen_mismatches
    else:
        assert keeping.state.verified and keeping.state.identity_claims  # (it did compare something)
        assert dropping.state.identity_claims == {} and dropping.state.seen_mismatches == []


@pytest.mark.parametrize("name", sorted(set(SCRIPTS) - set(NEVER_VERIFIED)))
def test_once_verified_the_record_holds_typed_values_only_in_its_free_text(name, agent, repo, monkeypatch):
    scenario, script = SCRIPTS[name]
    texts = [*identifier_spellings(repo), *_typed_values(script)]
    c = _play(agent, scenario, script)
    assert c.state.verified
    assert_holds_none_of(_outside_free_text(c.state), texts)
    # The check has teeth: before this change identity_claims still held them after verification.
    monkeypatch.setattr(policy, "_forget_identifier_values", lambda s: None)
    with pytest.raises(AssertionError):
        assert_holds_none_of(_outside_free_text(_play(agent, scenario, script, checked=False).state), texts)


def test_identifiers_stated_after_verification_are_not_stored(make):
    c = make()
    c.say("id", **MARGARET)
    established = (dict(c.state.field_status), c.state.policy_status, c.state.party_id, c.state.caller_role)
    so_far = len(c.state.events)
    c.say("here they are again, and I'm Ava Lopez", **AFTER_VERIFICATION)
    assert c.state.identity_claims == {} and c.state.seen_mismatches == []
    assert (c.state.field_status, c.state.policy_status, c.state.party_id, c.state.caller_role) == established
    later = {e["type"] for e in c.state.events[so_far:]}
    assert not later & {"identity_checked", "verification_strike", "role_change_ignored", "identity_verified"}


def test_until_verified_the_typed_values_are_kept_even_across_instances(make):
    c = make()
    c.say("my name", full_name="Margaret Chen")
    c.say("my date of birth", dob="1985-03-16")  # wrong: a strike, its value kept as a digest
    c.say("one moment")
    # Carried to another instance between turns, as a session store does.
    c.state = SessionState.from_record(json.loads(json.dumps(c.state.to_record())))
    assert c.state.identity_claims == {"full_name": "Margaret Chen", "dob": "1985-03-16"}
    assert len(c.state.seen_mismatches) == 1 and c.state.failed_attempts == 1
    c.say("my date of birth, again", dob="1985-03-16")  # still known: not a new strike
    assert c.state.failed_attempts == 1
    c.say("my phone", phone="650-521-2836")
    assert not c.state.verified and set(c.state.identity_claims) == {"full_name", "dob", "phone"}
    c.say("my SSN", id_last4="4472")  # the third match, with name and phone from earlier turns
    assert c.state.verified and c.state.party_id == "P9" and c.state.failed_attempts == 1
    assert c.state.identity_claims == {} and c.state.seen_mismatches == []
    assert c.state.field_status == {"full_name": "matched", "dob": "mismatched", "phone": "matched",
                                    "email": "not_provided", "id_last4": "matched"}
