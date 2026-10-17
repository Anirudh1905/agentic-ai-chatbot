"""The CI check (`make eval-check`): no model calls, no DeepEval, no credentials.

1. Runs the code-only suites (PII, approval policy) again, so those never rely on the report.
2. Fails if report.json is missing, or was made for different inputs than this checkout's:
   someone changed the code, prompts, knowledge base or evals without re-running `make eval`.
3. Compares the report with the one on `main`. A gating metric that regressed fails the check;
   an advisory one (a judged metric the judge isn't calibrated for) is only reported.

In GitHub Actions the comparison also goes to the job summary.
"""

import os
import sys
from dataclasses import asdict

from evals.report import (
    Metric,
    compare,
    fingerprint,
    load,
    load_baseline,
    summarize,
    table,
)
from evals.scoring import Outcome
from evals.suites import approvals, pii


def code_suites() -> tuple[dict[str, Metric], dict[str, dict[str, Outcome]]]:
    recall, clean = pii.run()
    cases = {
        "pii_recall": recall,
        "pii_no_false_positives": clean,
        "approval_policy": approvals.run(),
    }
    return {name: Metric(*summarize(c), gates=True) for name, c in cases.items()}, cases


def main() -> int:
    report = load()
    if report is None:
        print("No tests/evals/report.json. Run `make eval` and commit it.")
        return 1
    problems = []
    metrics = dict(report["metrics"])
    fresh, cases = code_suites()
    metrics.update({name: asdict(m) for name, m in fresh.items()})
    # Known gaps (an IPv6 address isn't detected, say) fail cases on main too; only a drop
    # from main's score blocks.
    failing = [
        f"{name}: {id}: {o.reason}"
        for name, c in cases.items()
        for id, o in c.items()
        if not o.passed
    ]
    if report["fingerprint"] != fingerprint():
        problems.append(
            "report.json was made for different code, prompts, knowledge base or evals than "
            "this checkout. Run `make eval` and commit the new report.json."
        )
    baseline = load_baseline()
    rows = compare(baseline, metrics)
    problems += [
        f"{r.metric} regressed: {r.baseline:.0%} on main, {r.now:.0%} now" for r in rows if r.blocks
    ]
    summary = "\n\n".join(
        [
            "## Evals",
            table(rows),
            "No baseline on main yet: nothing to compare with." if baseline is None else "",
            f"Ground truth: {report['ground_truth_by']}. Judge: {report['models']['judge_model']}.",
            "\n".join(f"- {p}" for p in problems) or "All checks passed.",
            ("Failing cases in the code-only suites:\n" + "\n".join(f"- {f}" for f in failing))
            if failing
            else "",
        ]
    )
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(summary + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
