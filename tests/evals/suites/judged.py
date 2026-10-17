"""Faithfulness, citations and correctness, scored by the judge through DeepEval.

- Faithfulness: every claim in the answer is supported by that turn's search results.
  `penalize_ambiguous_claims` makes a claim the results don't mention count against it; by
  default DeepEval only penalizes contradictions ("Shipping is always free" scored 1.0).
- Citations: what each citation vouches for (the text since the previous one) is supported by
  the results it names, and it names only results the model was given.
- Correctness: the answer agrees with the reference answer (knowledge base questions only; web
  answers change over time).
"""

from deepeval.metrics import FaithfulnessMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from evals.agent_runs import Result
from evals.citations import cited_spans
from evals.judge import JudgeModel
from evals.scoring import Incomplete, Outcome, complete

# A score at or above these passes. Faithfulness 1.0: no claim the results don't support.
FAITHFUL = 1.0
CORRECT = 0.8
# Correctness labels, for calibration: correct ≥ 0.8, partly ≥ 0.4, wrong below.
PARTLY = 0.4
ATTEMPTS = 3

CORRECTNESS_STEPS = [
    "List the facts in the expected output that answer the input question: numbers, time "
    "limits, conditions, and its yes/no conclusion if it has one. Details in parentheses are "
    "context, not required.",
    "Check whether the actual output states each of those facts in substance; wording may differ.",
    "Heavily penalize any statement in the actual output that contradicts the expected output.",
    "Penalize a missing fact in proportion to how much it changes the answer to the question "
    "asked. Do not penalize leaving out details the question didn't ask about.",
    "Do not penalize extra details that don't contradict the expected output, citations in "
    "square brackets, or offers to help further.",
]


async def faithfulness_score(
    judge: JudgeModel, question: str, output: str, contexts: list[str]
) -> Outcome:
    """FaithfulnessMetric, measured again (up to ATTEMPTS times) if the judge drops verdicts."""
    case = LLMTestCase(input=question, actual_output=output, retrieval_context=contexts)
    for _ in range(ATTEMPTS):
        metric = FaithfulnessMetric(model=judge, penalize_ambiguous_claims=True, async_mode=True)
        await metric.a_measure(case, _show_indicator=False)
        if complete(metric.claims, metric.verdicts):
            unsupported = [
                f"{c!r}: {v.reason}"
                for c, v in zip(metric.claims, metric.verdicts, strict=True)
                if str(getattr(v.verdict, "value", v.verdict)).lower() != "yes"
            ]
            return Outcome(
                metric.score >= FAITHFUL, metric.score, "; ".join(unsupported) or "all supported"
            )
    raise Incomplete(f"{len(metric.verdicts)} verdicts for {len(metric.claims)} claims")


async def faithfulness(
    judge: JudgeModel, question: str, answer: str, results: list[Result]
) -> Outcome:
    try:
        return await faithfulness_score(judge, question, answer, [r.content for r in results])
    except Incomplete as e:
        return Outcome.error(str(e))


async def citations(
    judge: JudgeModel, question: str, answer: str, results: list[Result]
) -> Outcome:
    """Passes if everything a citation vouches for is supported by the results it names. An
    answer that uses search results without citing any fails: the system prompt asks for a
    citation per claim."""
    cited = cited_spans(answer, results)
    if not cited:
        return Outcome(False, 0.0, "answers from search results without citing them")
    problems = []
    passed = 0
    for c in cited:
        if c.unknown:
            problems.append(f"{c.text!r} cites {c.unknown}, which no search returned")
            continue
        try:
            o = await faithfulness_score(judge, question, c.text, [s.content for s in c.sources])
        except Incomplete as e:
            return Outcome.error(str(e))
        if o.passed:
            passed += 1
        else:
            problems.append(f"{c.text!r}: {o.reason}")
    return Outcome(not problems, passed / len(cited), "; ".join(problems) or f"{len(cited)} ok")


async def correctness(judge: JudgeModel, question: str, answer: str, reference: str) -> Outcome:
    metric = GEval(
        name="Correctness",
        evaluation_steps=CORRECTNESS_STEPS,
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
        ],
        model=judge,
        async_mode=True,
    )
    case = LLMTestCase(input=question, actual_output=answer, expected_output=reference)
    await metric.a_measure(case, _show_indicator=False)
    return Outcome(metric.score >= CORRECT, metric.score, metric.reason or "")


def correctness_label(score: float) -> str:
    return "correct" if score >= CORRECT else "partly" if score >= PARTLY else "wrong"
