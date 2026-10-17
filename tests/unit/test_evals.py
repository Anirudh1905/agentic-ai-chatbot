"""The eval gate's own logic (tests/evals): fingerprint, comparison with main, re-run majority,
citation parsing, the CI check. No model calls and no DeepEval, like CI."""

from pathlib import Path
from typing import Any

import pytest
from evals import check, report
from evals.agent_runs import Result
from evals.citations import cited_spans
from evals.report import Metric, compare
from evals.scoring import Outcome, complete, majority
from evals.settings import eval_settings

from chatbot.config import Settings


def metrics(**scores: tuple[float, int, bool]) -> dict[str, Any]:
    return {name: {"score": s, "n": n, "gates": g} for name, (s, n, g) in scores.items()}


# --- fingerprint -----------------------------------------------------------------------


def test_the_fingerprint_changes_with_any_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    monkeypatch.setattr(report, "ROOT", tmp_path)
    monkeypatch.setattr(report, "files", lambda paths: ["a.py"])
    before = report.fingerprint()
    assert report.fingerprint() == before
    (tmp_path / "a.py").write_text("x = 2")
    assert report.fingerprint() != before


def test_the_report_itself_is_not_an_input() -> None:
    inputs = report.files(report.INPUTS)
    assert "tests/evals/report.json" not in inputs
    assert "tests/evals/golden.jsonl" in inputs and "src/chatbot/config.py" in inputs


# --- comparing with main -----------------------------------------------------------------


def test_a_drop_past_the_tolerance_blocks_and_one_within_it_does_not() -> None:
    main = {"metrics": metrics(routing=(0.95, 40, True), correctness=(0.90, 30, True))}
    # Routing: one case of 40 is 2.5 points, so 92.5% is within it; correctness drops 2 cases.
    now = metrics(routing=(0.925, 40, True), correctness=(0.8333, 30, True))
    rows = {r.metric: r for r in compare(main, now)}
    assert not rows["routing"].blocks
    assert rows["correctness"].blocks


def test_an_advisory_metric_is_reported_but_never_blocks() -> None:
    rows = compare(
        {"metrics": metrics(citations=(0.9, 30, False))}, metrics(citations=(0.5, 30, False))
    )
    assert rows[0].regressed and not rows[0].blocks


def test_code_only_suites_have_no_tolerance() -> None:
    main = {"metrics": metrics(pii_recall=(1.0, 21, True))}
    assert compare(main, metrics(pii_recall=(20 / 21, 21, True)))[0].blocks


def test_without_a_baseline_nothing_regresses() -> None:
    assert not any(r.regressed for r in compare(None, metrics(routing=(0.1, 40, True))))


# --- noise -------------------------------------------------------------------------------


def test_a_regression_needs_two_of_three_runs_to_fail() -> None:
    fail, ok, err = Outcome(False, 0.0, "no"), Outcome(True, 1.0), Outcome.error("timeout")
    assert majority([fail, ok, ok]).passed is True
    assert majority([fail, fail, ok]).passed is False
    assert majority([err, err, fail]).passed is None
    assert majority([fail, err, ok]).passed is True


def test_a_dropped_verdict_is_caught() -> None:
    assert complete(["a", "b"], ["yes", "no"])
    assert not complete(["a", "b"], ["yes"])  # DeepEval would score this 1.0


# --- citations ---------------------------------------------------------------------------

KB = Result("search_knowledge_base", "Refund Policy › Sale and Clearance Items", "Final sale.")
WEB = Result("web_search", "[REI returns](https://rei.example/returns)", "1 year.")


def test_a_citation_covers_the_text_since_the_previous_one() -> None:
    answer = (
        "Yes. Clearance items are final sale [Refund Policy › Sale and Clearance Items].\n"
        "REI allows a year [REI returns](https://rei.example/returns). Hope that helps!"
    )
    cited = cited_spans(answer, [KB, WEB])
    assert [(c.text, c.sources) for c in cited] == [
        ("Yes. Clearance items are final sale", [KB]),
        ("REI allows a year", [WEB]),
    ]


def test_a_citation_after_the_full_stop_and_an_unknown_one() -> None:
    answer = "Final sale. [Refund Policy › Sale and Clearance Items] Free [Shipping]."
    cited = cited_spans(answer, [KB])
    assert (cited[0].text, cited[0].sources, cited[0].unknown) == ("Final sale.", [KB], [])
    assert (cited[1].text, cited[1].unknown) == ("Free", ["Shipping"])


def test_full_stops_in_titles_and_abbreviations_are_kept_whole() -> None:
    web = Result("web_search", "[Donald J. Trump - The White House](https://wh.example/t)", "")
    answer = (
        "Orders before 1 p.m. Mountain Time ship the same day [Refund Policy › Sale and "
        "Clearance Items]. The president is Donald J. Trump "
        "[Donald J. Trump - The White House](https://wh.example/t)."
    )
    cited = cited_spans(answer, [KB, web])
    assert [c.sources for c in cited] == [[KB], [web]]
    assert cited[0].text == "Orders before 1 p.m. Mountain Time ship the same day"


def test_titles_with_brackets_wrapped_and_grouped_citations() -> None:
    web = Result("web_search", "[REI Return Policy [2026 Guide]](https://e.example/rei)", "")
    answer = (
        "REI gives 90 days [REI Return Policy [2026 Guide]](https://e.example/rei). "
        "Acme gives 30 [[x](https://e.example/rei)] [Refund Policy › Sale and Clearance Items]."
    )
    cited = cited_spans(answer, [KB, web])
    assert [(c.sources, c.unknown) for c in cited] == [([web], []), ([web, KB], [])]


def test_a_pii_placeholder_is_not_a_citation() -> None:
    assert cited_spans("Write to [REDACTED_EMAIL] for help.", [KB]) == []


# --- settings ----------------------------------------------------------------------------


def test_evals_use_the_defaults_whatever_dotenv_says(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTEXT_MAX_TOKENS", raising=False)
    settings = eval_settings()
    assert settings.context_max_tokens == Settings.model_fields["context_max_tokens"].default


def test_an_exported_setting_stops_the_evals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXT_MAX_TOKENS", "1000")
    with pytest.raises(SystemExit, match="CONTEXT_MAX_TOKENS"):
        eval_settings()


# --- the CI check ------------------------------------------------------------------------


def ci(monkeypatch: pytest.MonkeyPatch, fingerprint: str, baseline: dict[str, Any] | None) -> int:
    fresh, _ = check.code_suites()
    current = {
        "fingerprint": "abc",
        "ground_truth_by": "test",
        "models": {"judge_model": "gpt-4.1"},
        "metrics": {
            "routing": {"score": 0.9, "n": 40, "gates": True},
            **{name: vars(m) for name, m in fresh.items()},
        },
    }
    monkeypatch.setattr(check, "load", lambda: current)
    monkeypatch.setattr(check, "fingerprint", lambda: fingerprint)
    monkeypatch.setattr(check, "load_baseline", lambda: baseline)
    return check.main()


def test_the_check_fails_on_a_stale_report(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ci(monkeypatch, "abc", None) == 0
    assert ci(monkeypatch, "changed", None) == 1


def test_the_check_fails_on_a_regression_against_main(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ci(monkeypatch, "abc", {"metrics": metrics(routing=(0.9, 40, True))}) == 0
    assert ci(monkeypatch, "abc", {"metrics": metrics(routing=(1.0, 40, True))}) == 1


def test_the_code_only_suites_score_their_cases() -> None:
    fresh, cases = check.code_suites()
    assert set(fresh) == {"pii_recall", "pii_no_false_positives", "approval_policy"}
    assert all(isinstance(m, Metric) and m.n > 0 for m in fresh.values())
    assert len(cases["approval_policy"]) == 30
