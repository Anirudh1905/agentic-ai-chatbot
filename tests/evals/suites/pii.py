"""PII: does `redact_pii` catch every labelled value (recall), and leave alone what only looks
like PII (dates, prices, order numbers, Acme's own addresses)? Scored in code; runs in CI.

Each line of pii_cases.jsonl is {"text": ..., "pii": [{"type": ..., "value": ...}]}: the values
that must be redacted. A case with an empty list must come back unchanged.
"""

import json
from pathlib import Path

from chatbot.guardrails.pii import redact_pii
from evals.scoring import Outcome

CASES = Path(__file__).parents[1] / "pii_cases.jsonl"


def load() -> list[dict[str, object]]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def run() -> tuple[dict[str, Outcome], dict[str, Outcome]]:
    """(recall, false positives): one Outcome per labelled value, and one per case that has
    nothing to redact."""
    recall: dict[str, Outcome] = {}
    clean: dict[str, Outcome] = {}
    for case in load():
        text = str(case["text"])
        redacted, _ = redact_pii(text)
        values: list[dict[str, str]] = case["pii"]  # type: ignore[assignment]
        for v in values:
            caught = v["value"] not in redacted
            recall[f"{v['type']}: {v['value']}"] = Outcome(
                caught, float(caught), "redacted" if caught else f"left in: {redacted!r}"
            )
        if not values:
            kept = redacted == text
            clean[text] = Outcome(kept, float(kept), "unchanged" if kept else f"→ {redacted!r}")
    return recall, clean
