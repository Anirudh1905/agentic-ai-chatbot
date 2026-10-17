"""Routing: did the agent call exactly the tools the question needs? DeepEval's
ToolCorrectnessMetric with an exact match, which is scored in code (no judge)."""

from deepeval.metrics import ToolCorrectnessMetric
from deepeval.test_case import LLMTestCase, ToolCall

from evals.agent_runs import Run
from evals.golden import Case
from evals.scoring import Outcome


def score(case: Case, run: Run) -> Outcome:
    called = sorted(set(run.tools))  # calling a tool twice is still the right tool
    metric = ToolCorrectnessMetric(should_exact_match=True)
    metric.measure(
        LLMTestCase(
            input=case.question,
            actual_output=run.answer,
            tools_called=[ToolCall(name=n) for n in called],
            expected_tools=[ToolCall(name=n) for n in sorted(case.tools)],
        ),
        _show_indicator=False,
    )
    return Outcome(metric.score == 1, metric.score, f"wanted {sorted(case.tools)}, got {called}")
