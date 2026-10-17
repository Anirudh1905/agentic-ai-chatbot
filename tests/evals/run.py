"""`make eval`: every suite, against the local stack and the model gateway. Writes report.json.

Needs Postgres with the sample docs ingested (`make eval` ingests them first) and the model
credentials in `.env`. Every other setting is at its default (settings.py).

    make eval
"""

import asyncio
import json
import sys
from typing import Any

from evals.agent_runs import Agent, Run
from evals.check import code_suites
from evals.golden import Case, load_cases
from evals.judge import JudgeModel
from evals.report import (
    CALIBRATION,
    MIN_AGREEMENT,
    REPORT,
    Metric,
    baseline_passed,
    build,
    compare,
    judge_fingerprint,
    load,
    load_baseline,
    summarize,
    table,
)
from evals.scoring import Outcome, majority
from evals.settings import eval_settings
from evals.suites import judged, retrieval, routing

JUDGED = ("faithfulness", "citations", "correctness")
# A case that passed on main and fails now is run this many more times; it fails on a majority.
RERUNS = 2
# Tries per question when the agent or judge errors (a gateway timeout, usually).
ATTEMPTS = 3


def metrics_for(case: Case) -> list[str]:
    return ["routing", "faithfulness", "citations"] + (["correctness"] if case.reference else [])


async def score(judge: JudgeModel, case: Case, run: Run) -> dict[str, Outcome]:
    out = {"routing": routing.score(case, run)}
    if run.results:  # nothing to be faithful to, or to cite, without search results
        out["faithfulness"], out["citations"] = await asyncio.gather(
            judged.faithfulness(judge, case.question, run.answer, run.results),
            judged.citations(judge, case.question, run.answer, run.results),
        )
    if case.reference:
        out["correctness"] = await judged.correctness(
            judge, case.question, run.answer, case.reference
        )
    return out


async def main() -> int:
    settings = eval_settings()
    baseline = load_baseline()
    cases = load_cases()
    print(f"Retrieval: {sum(bool(c.expect) for c in cases)} questions…", flush=True)
    by_metric: dict[str, dict[str, Outcome]] = {"retrieval": await retrieval.run(settings, cases)}

    judge = JudgeModel(settings)
    async with Agent(settings) as agent:
        runnable = [c for c in cases if set(c.tools) <= agent.available]

        async def one(case: Case) -> dict[str, Outcome]:
            for attempt in range(ATTEMPTS):
                try:
                    return await score(judge, case, await agent.run(case.question))
                except Exception as e:  # the gateway stalls under load; try the case again
                    error = e
                    await asyncio.sleep(5 * (attempt + 1))
            # Still failing: an error, counted as neither a pass nor a fail.
            return {m: Outcome.error(f"{type(error).__name__}: {error}") for m in metrics_for(case)}

        print(f"Agent and judge: {len(runnable)} questions…", flush=True)
        scored = dict(
            zip([c.id for c in runnable], await asyncio.gather(*map(one, runnable)), strict=True)
        )
        regressed = [
            c
            for c in runnable
            if any(
                o.passed is False and baseline_passed(baseline, m, c.id)
                for m, o in scored[c.id].items()
            )
        ]
        if regressed:
            print(f"Re-running {len(regressed)} that passed on main…", flush=True)
            reruns = await asyncio.gather(
                *(asyncio.gather(*(one(c) for _ in range(RERUNS))) for c in regressed)
            )
            for case, extra in zip(regressed, reruns, strict=True):
                scored[case.id] = {
                    m: majority([o, *(e[m] for e in extra if m in e)])
                    for m, o in scored[case.id].items()
                }

    for case_id, outcomes in scored.items():
        for metric, outcome in outcomes.items():
            by_metric.setdefault(metric, {})[case_id] = outcome
    by_metric.update(code_suites()[1])

    calibration = load(CALIBRATION)
    current = bool(calibration and calibration["fingerprint"] == judge_fingerprint())
    metrics = {}
    for name, outcomes in by_metric.items():
        if name not in JUDGED:
            metrics[name] = Metric(*summarize(outcomes), gates=True)
            continue
        agreement = (calibration or {}).get("agreement", {}).get(name)
        note = (
            ""
            if current
            else "calibration out of date: run `make eval-calibrate`"
            if calibration
            else "not calibrated"
        )
        gates = current and agreement is not None and agreement >= MIN_AGREEMENT
        metrics[name] = Metric(*summarize(outcomes), gates=gates, agreement=agreement, note=note)

    models: dict[str, Any] = {
        "model": settings.model,
        "reasoning_effort": settings.reasoning_effort,
        "judge_model": settings.judge_model,
        "summary_model": settings.summary_model,
        "embedding_model": settings.embedding_model,
        "reranker_model": settings.reranker_model,
    }
    report = build(metrics, by_metric, models)
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")

    rows = compare(baseline, report["metrics"])
    print(table(rows))
    for name, outcomes in by_metric.items():
        for case_id, o in outcomes.items():
            if not o.passed:
                print(f"  {'ERROR' if o.passed is None else 'FAIL '} {name}: {case_id}")
                print(f"    {o.reason}")
    print(f"Wrote {REPORT.name}. Commit it with the PR.")
    return 1 if any(r.blocks for r in rows) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
