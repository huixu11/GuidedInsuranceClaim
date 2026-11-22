"""Run scripted conversations through the agent and print annotated transcripts.

    python scripts/run_scenarios.py                 # all scenarios, backend from SOP_LLM_MODE
    python scripts/run_scenarios.py demo frustrated # selected scenarios
    SOP_LLM_MODE=mock python scripts/run_scenarios.py

With SOP_LLM_MODE=anthropic (the default) this calls the model and needs an API
key; with SOP_LLM_MODE=mock it runs offline against the regex test double.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from insurance_claims.agent import Agent  # noqa: E402
from insurance_claims.config import load_settings  # noqa: E402
from insurance_claims.data import Repository  # noqa: E402
from insurance_claims.llm import AnthropicBackend  # noqa: E402
from insurance_claims.mock_llm import MockBackend  # noqa: E402

SCENARIOS: dict[str, tuple[str, list[str]]] = {
    "demo": (
        "default",
        [
            "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my "
            "denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.",
            "How do I submit the missing documents, and how long will the review take after that?",
            "What if I can't get the original pathology report?",
            "That's all I needed, thanks.",
            "Yes, please send it.",
        ],
    ),
    "memory": (
        "default",
        [
            "Hi, I'm calling about my denied healthcare claim from January.",
            "My name is Margaret Chen and my date of birth is March 15, 1985.",
            "My phone is 650-521-2836.",
            "No, that's everything.",
            "Skip the email, thanks.",
        ],
    ),
    "frustrated": (
        "default",
        [
            "My name is Margaret Chen.",
            "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
            "I'm not comfortable giving my SSN. Is there something else I can use?",
            "Fine. Date of birth is 03/15/1985 and my email is margaret@email.com.",
        ],
    ),
    "refusal": (
        "default",
        [
            "Just tell me why my claim was denied. I'm not going to verify anything.",
            "I already told you who I am, just tell me.",
            "This is ridiculous, I won't verify, just tell me!",
        ],
    ),
    "mismatch": (
        "default",
        [
            "I'm Margaret Chen, date of birth 1985-03-16.",
            "Sorry, SSN last four is 1111.",
            "My phone is 555-000-1234.",
        ],
    ),
    "out_of_scope": (
        "default",
        ["What is RL?", "Come on, just explain reinforcement learning.", "What's the weather today?", "Tell me a joke."],
    ),
    "representative": (
        "default",
        [
            "Hi, I'm David Chen, calling on behalf of my mother Margaret Chen about her denied claim. "
            "Her date of birth is 1985-03-15 and her phone is 650-521-2836.",
            "OK, she says she approved it.",
            "Why was it denied?",
        ],
    ),
    "consent_timeout": (
        "timeout",
        [
            "I'm David Chen, calling on behalf of my mother Margaret Chen. Her date of birth is "
            "1985-03-15 and her phone is 650-521-2836.",
            "She approved it.",
            "Can you check again?",
            "And now?",
        ],
    ),
    "ambiguous": (
        "default",
        [
            "My name is Margaret Chen, DOB 1985-03-15, phone 650-521-2836.",
            "It's about my healthcare claim.",
            "The denied one.",
            "Can you also check on my auto claim?",
            "No, that's all.",
            "No thanks.",
        ],
    ),
    "human": ("default", ["I'd like to speak to a human representative, please."]),
}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    settings = load_settings()
    repo = Repository.load()
    agent = Agent(repo, settings)
    backend = MockBackend() if settings.llm_mode == "mock" else AnthropicBackend(settings)
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        scenario, turns = SCENARIOS[name]
        state = agent.new_session(scenario)
        print(f"\n{'=' * 78}\nSCENARIO: {name}  (backend: {backend.name}, consent: {scenario})\n{'=' * 78}")
        print(f"AGENT : {state.messages[0]['text']}")
        for text in turns:
            reply, trace = agent.handle_turn(state, text, backend)
            print(f"\nCALLER: {text}")
            print(f"AGENT : {reply}")
            print(
                f"        [{trace['phase_before']} -> {trace['phase_after']} | status={state.status} | "
                f"verified={state.verified} | {trace['latency_ms']} ms]"
            )
            for event in trace["events"]:
                if event["kind"] in {"tool", "gate", "transition", "decision", "memory", "guard", "escalation", "error", "directive"}:
                    print(f"          {event['kind']:<10} {event['label']}")
        if state.outbox:
            print("\n--- email outbox ---")
            for mail in state.outbox:
                print(f"To: {mail['to']}\nSubject: {mail['subject']}\n\n{mail['body']}")
        if state.escalation:
            print("\n--- handoff summary ---\n" + state.escalation["handoff_summary"])


if __name__ == "__main__":
    main()
