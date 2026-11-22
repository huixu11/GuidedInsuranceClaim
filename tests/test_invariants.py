"""Property test: whatever the model 'understands', the SOP invariants hold.

The structured reading is untrusted input. This drives many random multi-turn
conversations with arbitrary schema-valid readings (including contradictory
flags, other people's claim numbers, role flips and mixtures of correct and
wrong identifiers) and checks after every turn that no gate was bypassed and
nothing crashed.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from insurance_claims import identity
from insurance_claims.state import Phase
from insurance_claims.understanding import (
    CASE_TYPES,
    CLAIM_STATUSES,
    EMAIL_CHOICES,
    EMOTIONS,
    INTENSITIES,
    INTENTS,
    REFUSALS,
    ROLES,
    SCOPES,
    YES_NO,
)

from .conftest import Conversation, ScriptedBackend, reading

FIELD_OF = {"full_name": "name", "dob": "dob", "phone": "phone", "email": "email", "id_last4": "id_last4"}


PERSONAS = ("honest", "representative", "chaotic", "refuser")


def random_reading(rng: random.Random, repo: Any, persona: str = "chaotic", who: Any = None) -> dict[str, Any]:
    """One arbitrary schema-valid reading.

    honest         - mostly the correct identifiers of one policyholder; reaches the later phases
    representative - David Chen calling for Margaret Chen, sometimes flipping role or identity
    chaotic        - anything: identifiers of several people, wrong values, every flag
    """
    holder = who or rng.choice(repo.policyholders)
    other = rng.choice(repo.policyholders)
    wrong = {"full_name": "Jordan Example", "dob": "1970-01-01", "phone": "555-010-0000",
             "email": "nobody@example.org", "id_last4": "0000"}
    # refuser: hardly ever gives an identifier and keeps demanding answers
    p_right, p_other, p_wrong = {"honest": (0.35, 0.0, 0.02), "representative": (0.35, 0.04, 0.02),
                                 "chaotic": (0.30, 0.08, 0.10), "refuser": (0.03, 0.0, 0.0)}[persona]
    values: dict[str, Any] = {}
    for f in identity.PII_FIELDS:
        roll = rng.random()
        if roll < p_right:
            values[f] = getattr(holder, FIELD_OF[f])
        elif roll < p_right + p_other:
            values[f] = getattr(other, FIELD_OF[f])  # someone else's identifier
        elif roll < p_right + p_other + p_wrong:
            values[f] = wrong[f]
    if rng.random() < 0.2:
        values["policy_number"] = rng.choice(repo.policyholders).policy_number
    flag = lambda p: rng.random() < p  # noqa: E731
    calm = persona != "chaotic"
    if persona == "representative":
        role = rng.choices(ROLES, weights=[1, 8, 3])[0]
        rep_name = rng.choices(["David Chen", "", "Bob Stranger"], weights=[8, 3, 1])[0]
    elif persona == "honest":
        role, rep_name = rng.choice(["policyholder", "unknown"]), ""
    else:
        role, rep_name = rng.choice(ROLES), rng.choice(["", "", "David Chen", "Bob Stranger"])
    return reading(
        role=role,
        rep_name=rep_name,
        # (a stated relationship is itself a sign of a third party, so honest callers give none)
        relationship="" if persona in {"honest", "refuser"} else rng.choice(["", "son"]),
        case_id=rng.choice(["", "", "", ""] + [c["case_id"] for c in repo.claims] + ["CL-9999"]),
        case_type=rng.choice(CASE_TYPES),
        status=rng.choice(CLAIM_STATUSES),
        time_reference=rng.choice(["", "", "January", "2025", "last month"]),
        intent=rng.choice(INTENTS),
        scope=rng.choices(SCOPES, weights=[30, 1, 1] if calm else [8, 1, 1])[0],
        out_of_scope_topic=rng.choice(["", "RL"]),
        emotion=rng.choice(EMOTIONS),
        emotion_intensity=rng.choice(INTENSITIES),
        refusal=rng.choices(
            REFUSALS,
            weights=[3, 6, 1, 1, 1] if persona == "refuser" else [40, 1, 1, 1, 1] if calm else [10, 1, 1, 1, 1],
        )[0],
        refused_fields=rng.sample(list(identity.PII_FIELDS), k=rng.choice([0, 0, 0, 1])),
        asks_why=flag(0.1),
        requests_human=flag(0.005 if calm else 0.03),
        wants_to_end=flag(0.12),
        wants_different_claim=flag(0.12),
        yes_no=rng.choice(YES_NO),
        email_summary_choice=rng.choices(EMAIL_CHOICES, weights=[6, 1, 1])[0],
        email_to_other_address=flag(0.05),
        notes=rng.choice([[], ["prefers mornings"]]),
        **values,
    )


def check_invariants(convo: Conversation, repo: Any, settings: Any, before: dict[str, Any]) -> None:
    s = convo.state
    owned = {c["case_id"] for c in repo.claims_for(s.party_id)} if s.verified else set()

    if not s.verified:
        # Nothing behind the gate has been touched.
        assert s.phase == Phase.VERIFY_ID
        assert s.party_id is None and s.claims_on_file is None and s.selected_case_id is None
        assert s.grounding == [] and s.outbox == [] and s.cases_discussed == []
        assert not any(e["kind"] == "tool" and ("list_claims" in e["label"] or "get_claim" in e["label"]
                                                 or "get_document" in e["label"] or "send_summary" in e["label"])
                       for e in convo.last_trace["events"])
    else:
        # Verification means 3 identifiers of ONE record matched what the caller supplied...
        holder = repo.policyholder(s.party_id)
        assert holder is not None
        result = identity.evaluate(s.identity_claims, repo)
        assert result.party_id == s.party_id and result.matched_count >= settings.required_id_matches
        # ...and, for a representative, the person named NOW is on file for THIS policyholder
        # and the consent that was approved was requested for exactly that pair.
        if s.caller_role == "representative":
            assert s.rep_on_file is True and s.consent_status == "approved"
            assert s.consent_party_id == s.party_id
            assert any(identity.names_match(s.rep_name, r["rep_name"])
                       for r in repo.representatives_for(s.party_id))
            assert s.consent_rep == identity.normalize("full_name", s.rep_name)
        else:
            # No sign of a third party was ever recorded on a call verified as the policyholder.
            assert not s.rep_name and not s.rep_relationship
        assert before["verified"] or s.phase != Phase.VERIFY_ID

    if before["verified"]:
        assert s.verified and s.party_id == before["party_id"]  # verification is never re-pointed
        assert s.phase != Phase.VERIFY_ID  # and there is no way back around the gate

    if s.selected_case_id is not None:
        assert s.selected_case_id in owned
    if s.phase == Phase.PROCESS_CASE:
        assert s.selected_case_id in owned
    assert set(s.cases_discussed) <= owned
    assert set(s.candidates) <= owned

    # Email only on an explicit yes, only to the address on file (masked in the record).
    for mail in s.outbox:
        assert s.verified and mail["to"] == identity.mask_email(repo.policyholder(s.party_id).email)
    if len(s.outbox) > before["outbox"]:
        assert s.email_decision == "send"

    if s.status == "escalated":
        assert s.escalation and s.escalation["handoff_summary"]
    if before["status"] == "escalated":
        assert s.status == "escalated" and s.phase == before["phase"] and s.verified == before["verified"]

    assert s.failed_attempts <= settings.max_failed_attempts
    assert s.refusal_count <= settings.max_persuasion_attempts + 1
    # The trace and state view never carry identifier values.
    blob = str(convo.last_trace) + str(s.to_view(settings))
    for h in repo.policyholders:
        assert h.dob not in blob and h.email not in blob and h.phone[-10:] not in blob


def play(seed: int, agent: Any, repo: Any, settings: Any, turns: int = 30) -> Conversation:
    rng = random.Random(seed)
    persona = PERSONAS[seed % len(PERSONAS)]
    who = repo.policyholder("P9") if persona == "representative" else rng.choice(repo.policyholders)
    convo = Conversation(agent, ScriptedBackend(), rng.choice(["default", "default", "timeout"]))
    for _ in range(turns):
        s = convo.state
        before = {"verified": s.verified, "party_id": s.party_id, "status": s.status,
                  "phase": s.phase, "outbox": len(s.outbox)}
        convo.say("fuzz", **random_reading(rng, repo, persona, who))
        check_invariants(convo, repo, settings, before)
    return convo


@pytest.mark.parametrize("seed", range(150))
def test_random_conversations_never_break_the_sop(seed, agent, repo, settings):
    play(seed, agent, repo, settings)


def test_the_random_conversations_actually_exercise_every_phase(agent, repo, settings):
    """Guards the property test itself: it must reach the states it claims to protect."""
    reached: set[str] = set()
    for seed in range(150):
        s = play(seed, agent, repo, settings).state
        reached.add(s.phase.value)
        reached |= {h["to"] for h in s.phase_history}
        if s.outbox:
            reached.add("email_sent")
        if s.verified and s.caller_role == "representative":
            reached.add("representative_verified")
        if s.status == "escalated":
            reached.add("escalated:" + s.escalation["reason"])
        if s.status == "completed":
            reached.add("completed")
    assert reached >= {
        "VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS", "completed", "email_sent",
        "representative_verified", "escalated:caller_requested", "escalated:verification_failed",
        "escalated:verification_refused", "escalated:repeated_out_of_scope",
    }, reached
