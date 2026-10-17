"""The eval report (report.json): a score per metric and an outcome per case, the fingerprint of
what was measured, and the comparison with the baseline on `main`. No model calls, no DeepEval:
CI uses it too.
"""

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chatbot.config import Settings
from evals.scoring import Outcome

ROOT = Path(__file__).parents[2]
REPORT = Path(__file__).with_name("report.json")
CALIBRATION = Path(__file__).with_name("calibration.json")
BASELINE_REF = "origin/main"

# Pinned in the Makefile too (`uv run --with deepeval==...`); judge.py checks it's what's
# installed. A new version can change the metrics' prompts.
DEEPEVAL_VERSION = "4.3.1"

# Everything that can change an answer or a score.
INPUTS = ["src/chatbot", "data/sample", "tests/evals", "pyproject.toml", "uv.lock"]
NOT_INPUTS = {"tests/evals/report.json"}
# What the judge's agreement depends on.
JUDGE_INPUTS = [
    "tests/evals/judge.py",
    "tests/evals/suites/judged.py",
    "tests/evals/citations.py",
    "tests/evals/calibrate.py",
    "tests/evals/calibration.jsonl",
]

# A judged metric gates only when the judge agrees with the labelled answers this often.
MIN_AGREEMENT = 0.9
# A metric regresses when it drops below the baseline by more than this, or one case: answers
# and verdicts vary between runs. The code-only suites don't, so any drop there is a regression.
TOLERANCE = 0.02
EXACT = {"pii_recall", "pii_no_false_positives", "approval_policy"}
GROUND_TRUTH_BY = "Claude (model-written labels and reference answers, not human-labelled)"


def files(paths: list[str]) -> list[str]:
    """Tracked files and new ones not yet committed (not ignored ones), so an uncommitted edit
    counts too."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return sorted({f for f in listed if f not in NOT_INPUTS and (ROOT / f).is_file()})


def digest(paths: list[str], *extra: str) -> str:
    h = hashlib.sha256()
    for path in files(paths):
        h.update(path.encode() + b"\0" + (ROOT / path).read_bytes() + b"\0")
    for value in extra:
        h.update(value.encode() + b"\0")
    return h.hexdigest()


def fingerprint() -> str:
    return digest(INPUTS, DEEPEVAL_VERSION)


def judge_fingerprint() -> str:
    judge = str(Settings.model_fields["judge_model"].default)
    return digest(JUDGE_INPUTS, DEEPEVAL_VERSION, judge)


@dataclass
class Metric:
    score: float
    n: int
    gates: bool
    agreement: float | None = None  # judged metrics: agreement with the calibration labels
    note: str = ""


def summarize(outcomes: dict[str, Outcome]) -> tuple[float, int]:
    """Share of cases that passed, leaving out the ones that couldn't be scored."""
    scored = [o for o in outcomes.values() if o.passed is not None]
    return (sum(bool(o.passed) for o in scored) / len(scored) if scored else 0.0), len(scored)


def build(
    metrics: dict[str, Metric], cases: dict[str, dict[str, Outcome]], models: dict[str, Any]
) -> dict[str, Any]:
    return {
        "fingerprint": fingerprint(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": models,
        "deepeval_version": DEEPEVAL_VERSION,
        "ground_truth_by": GROUND_TRUTH_BY,
        "metrics": {name: asdict(m) for name, m in metrics.items()},
        "errors": sum(o.passed is None for c in cases.values() for o in c.values()),
        "cases": {
            metric: {id: {**asdict(o), "reason": o.reason[:400]} for id, o in sorted(c.items())}
            for metric, c in cases.items()
        },
    }


def load(path: Path = REPORT) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def load_baseline() -> dict[str, Any] | None:
    """The report on `main`, or None if there isn't one yet (the first PR)."""
    for ref in (BASELINE_REF, "main"):
        shown = subprocess.run(
            ["git", "show", f"{ref}:{REPORT.relative_to(ROOT)}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if shown.returncode == 0:
            return dict(json.loads(shown.stdout))
    return None


def baseline_passed(baseline: dict[str, Any] | None, metric: str, case_id: str) -> bool:
    case = (baseline or {}).get("cases", {}).get(metric, {}).get(case_id)
    return bool(case and case["passed"])


@dataclass
class Row:
    metric: str
    baseline: float | None
    now: float
    n: int
    gates: bool
    regressed: bool

    @property
    def blocks(self) -> bool:
        return self.gates and self.regressed


def compare(baseline: dict[str, Any] | None, metrics: dict[str, Any]) -> list[Row]:
    rows = []
    for name, m in metrics.items():
        base = (baseline or {}).get("metrics", {}).get(name)
        before = base["score"] if base else None
        allowed = 0.0 if name in EXACT else max(TOLERANCE, 1 / m["n"]) if m["n"] else TOLERANCE
        regressed = before is not None and m["score"] < before - allowed - 1e-9
        rows.append(Row(name, before, m["score"], m["n"], m["gates"], regressed))
    return rows


def table(rows: list[Row]) -> str:
    def pct(x: float | None) -> str:
        return "–" if x is None else f"{x:.0%}"

    lines = ["| Metric | main | this PR | n | Gates | |", "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        change = "" if r.baseline is None else f"{(r.now - r.baseline) * 100:+.0f} pts"
        status = "❌ regressed" if r.blocks else "⚠️ regressed" if r.regressed else change
        gates = "yes" if r.gates else "advisory"
        lines.append(
            f"| {r.metric} | {pct(r.baseline)} | {pct(r.now)} | {r.n} | {gates} | {status} |"
        )
    return "\n".join(lines)
