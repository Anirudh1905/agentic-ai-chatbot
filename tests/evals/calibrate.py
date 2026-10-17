"""`make eval-calibrate`: how often the judge agrees with the labelled answers in
calibration.jsonl. A judged metric gates PRs only when the agreement is at least 90%.

Each line: {"id", "question", "answer", "results": [{"tool", "cite_as", "content"}],
"reference": str or null, "labels": {"faithfulness": bool, "citations": bool,
"correctness": "correct" | "partly" | "wrong"}}, any label optional. The answers are fixed (no
agent runs), so only the judge varies. The labels were written by Claude, not a person.

Writes calibration.json: agreement per metric, every disagreement with the judge's reason, and
the fingerprint of the judge (model, prompts, DeepEval version, this set) it holds for.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.agent_runs import Result
from evals.judge import JudgeModel
from evals.report import CALIBRATION, MIN_AGREEMENT, judge_fingerprint
from evals.scoring import Outcome
from evals.settings import eval_settings
from evals.suites import judged

ITEMS = Path(__file__).with_name("calibration.jsonl")


async def judge_item(judge: JudgeModel, item: dict[str, Any]) -> list[dict[str, Any]]:
    results = [Result(**r) for r in item["results"]]
    verdicts = []
    for metric, label in item["labels"].items():
        try:
            o, said = await judge_one(judge, item, metric, results)
        except Exception as e:  # still failing after the judge's retries
            o, said = Outcome.error(f"{type(e).__name__}: {e}"), None
        verdicts.append(
            {"id": item["id"], "metric": metric, "label": label, "judge": said, "reason": o.reason}
        )
    return verdicts


async def judge_one(
    judge: JudgeModel, item: dict[str, Any], metric: str, results: list[Result]
) -> tuple[Outcome, object]:
    """The judge's outcome, and its verdict in the label's terms."""
    question, answer = item["question"], item["answer"]
    if metric == "faithfulness":
        o = await judged.faithfulness(judge, question, answer, results)
        return o, o.passed
    if metric == "citations":
        o = await judged.citations(judge, question, answer, results)
        return o, o.passed
    o = await judged.correctness(judge, question, answer, item["reference"])
    return o, judged.correctness_label(o.score) if o.score is not None else None


async def main() -> int:
    judge = JudgeModel(eval_settings())
    items = [json.loads(line) for line in ITEMS.read_text().splitlines() if line.strip()]
    print(f"Judging {len(items)} labelled answers…", flush=True)
    verdicts = [
        v for vs in await asyncio.gather(*(judge_item(judge, i) for i in items)) for v in vs
    ]
    agreement, n = {}, {}
    for metric in sorted({v["metric"] for v in verdicts}):
        scored = [v for v in verdicts if v["metric"] == metric and v["judge"] is not None]
        n[metric] = len(scored)
        agreement[metric] = sum(v["judge"] == v["label"] for v in scored) / len(scored)
    out = {
        "fingerprint": judge_fingerprint(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agreement": agreement,
        "n": n,
        "errors": sum(v["judge"] is None for v in verdicts),
        "disagreements": [
            v for v in verdicts if v["judge"] is not None and v["judge"] != v["label"]
        ],
    }
    CALIBRATION.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    for metric, a in agreement.items():
        verdict = "gates" if a >= MIN_AGREEMENT else "advisory"
        print(f"{metric}: {a:.0%} agreement on {n[metric]} ({verdict})")
    for d in out["disagreements"]:
        print(f"  {d['metric']} {d['id']}: labelled {d['label']}, judge {d['judge']}")
        print(f"    {d['reason'][:300]}")
    print(f"Wrote {CALIBRATION.name}. Commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
