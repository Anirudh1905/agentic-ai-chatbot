"""Approval policy: does `sensitive_reason` send exactly the right web searches for approval
(internal terms, order numbers) and let the rest through? Scored in code; runs in CI.

Each line of approval_cases.jsonl is {"query": ..., "sensitive": true or false}. The terms are
HITL_SENSITIVE_TERMS at their default.
"""

import json
from pathlib import Path

from chatbot.agent.approvals import sensitive_reason
from chatbot.config import Settings
from evals.scoring import Outcome

CASES = Path(__file__).parents[1] / "approval_cases.jsonl"


def run() -> dict[str, Outcome]:
    terms = Settings.model_fields["hitl_sensitive_terms"].default
    outcomes = {}
    for line in CASES.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        reason = sensitive_reason(case["query"], terms)
        right = (reason is not None) == case["sensitive"]
        want = "approval" if case["sensitive"] else "no approval"
        outcomes[case["query"]] = Outcome(right, float(right), f"wanted {want}; got {reason}")
    return outcomes
