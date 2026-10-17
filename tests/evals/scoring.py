"""How one case's result is recorded, and how re-runs settle a regression. No model calls."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    """One metric on one case. `passed` is None when it couldn't be scored (an error)."""

    passed: bool | None
    score: float | None = None
    reason: str = ""

    @staticmethod
    def error(reason: str) -> "Outcome":
        return Outcome(None, None, reason)


def majority(outcomes: Sequence[Outcome]) -> Outcome:
    """Settle a case run several times (a regression gets 3 runs): it fails only if most runs
    fail, so one noisy answer or verdict doesn't block a PR."""
    failed = [o for o in outcomes if o.passed is False]
    errors = [o for o in outcomes if o.passed is None]
    passed = [o for o in outcomes if o.passed]
    half = len(outcomes) / 2
    if len(failed) > half:
        return failed[0]
    if len(errors) > half:
        return errors[0]
    if passed:
        return passed[0]
    return failed[0] if failed else errors[0]


class Incomplete(Exception):
    """The judge returned fewer verdicts than there are claims."""


def complete(claims: Sequence[object], verdicts: Sequence[object]) -> bool:
    """DeepEval scores the verdicts it gets, so a dropped verdict can turn an unsupported claim
    into a perfect score (one run in three in the spike). Such a result is measured again."""
    return len(verdicts) == len(claims)
