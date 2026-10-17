"""Evals: how good the bot is, measured on golden sets and gated on every PR (Phase 7).

- `make eval` (`run.py`) runs every suite against the local stack and the model gateway and
  writes `report.json`, which is committed with the PR.
- `make eval-check` (`check.py`, in CI) runs the code-only suites again, checks the report is
  for the code being merged, and compares it with the report on `main`.
- `make eval-calibrate` (`calibrate.py`) measures how often the judge agrees with labelled
  answers. A judged metric only gates once it agrees on at least 90% of them.

The suites are in `suites/`. Retrieval, routing, PII and the approval policy are scored in code;
faithfulness, citations and correctness by a judge model (`JUDGE_MODEL`) through DeepEval.
"""
