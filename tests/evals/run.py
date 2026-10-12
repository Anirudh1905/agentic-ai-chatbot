"""Phase 2 evals: retrieval recall@k and tool routing accuracy on golden.jsonl.

Needs the stack running (Postgres with the sample docs ingested, and a model API key).

    uv run python tests/evals/run.py                  # retrieval + routing
    uv run python tests/evals/run.py --retrieval-only # no chat model calls
    uv run python tests/evals/run.py --retrieval-only --no-rerank  # compare without the reranker
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from chatbot.config import get_settings
from chatbot.db import open_pool
from chatbot.embeddings import build_embeddings
from chatbot.graph.builder import build_graph
from chatbot.tools.rag import retrieve
from chatbot.tools.registry import build_tools
from chatbot.tools.rerank import build_reranker

GOLDEN = Path(__file__).with_name("golden.jsonl")
CONCURRENCY = 4


@dataclass
class Case:
    question: str
    tools: list[str]
    expect: dict[str, Any] | None


def load_cases() -> list[Case]:
    lines = GOLDEN.read_text().splitlines()
    return [Case(**{"expect": None, **json.loads(line)}) for line in lines if line.strip()]


def matches(hit_doc: str, hit_heading: str | None, expect: dict[str, Any]) -> bool:
    if not hit_doc.endswith(expect["doc"]):
        return False
    return expect["heading"] is None or hit_heading == expect["heading"]


async def eval_retrieval(cases: list[Case], rerank: bool) -> float:
    settings = get_settings()
    pool = await open_pool(settings)
    embeddings = build_embeddings(settings)
    reranker = build_reranker(settings) if rerank else None
    k = settings.retrieval_top_k
    graded = [c for c in cases if c.expect]
    ranks: list[int | None] = []
    try:
        for case in graded:
            assert case.expect is not None
            results = await retrieve(pool, embeddings, settings, case.question, reranker)
            rank = next(
                (i for i, r in enumerate(results, 1) if matches(r.doc_id, r.heading, case.expect)),
                None,
            )
            ranks.append(rank)
            if rank != 1:
                got = [r.cite_as for r in results[:3]]
                label = "MISS " if rank is None else f"RANK{rank}"
                print(f"  {label} {case.question}\n        wanted {case.expect}, got {got}")
    finally:
        await pool.close()
    n = len(graded)
    recall = sum(r is not None for r in ranks) / n
    top1 = sum(r == 1 for r in ranks) / n
    mrr = sum(1 / r for r in ranks if r) / n
    mode = f"reranker {settings.reranker_model}" if reranker else "no reranker"
    print(f"Retrieval ({mode}): recall@{k} {recall:.0%}, recall@1 {top1:.0%}, MRR {mrr:.2f}")
    return recall


async def eval_routing(cases: list[Case]) -> float:
    settings = get_settings()
    pool = await open_pool(settings)
    tools = build_tools(settings, pool, build_embeddings(settings), build_reranker(settings))
    available = {t.name for t in tools}
    graph = build_graph(settings, tools=tools)
    runnable = [c for c in cases if set(c.tools) <= available]
    skipped = len(cases) - len(runnable)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def called_tools(case: Case) -> set[str]:
        async with semaphore:
            result = await graph.ainvoke(
                {"messages": [HumanMessage(case.question)]},
                config={"recursion_limit": settings.max_agent_steps},
            )
        return {
            tc["name"]
            for m in result["messages"]
            if isinstance(m, AIMessage)
            for tc in m.tool_calls
        }

    try:
        results = await asyncio.gather(*(called_tools(c) for c in runnable))
    finally:
        await pool.close()

    correct = 0
    for case, got in zip(runnable, results, strict=True):
        if got == set(case.tools):
            correct += 1
        else:
            print(
                f"  WRONG {case.question}\n        wanted {sorted(case.tools)}, got {sorted(got)}"
            )
    accuracy = correct / len(runnable)
    note = f" ({skipped} skipped: tool not configured)" if skipped else ""
    print(f"Tool routing accuracy: {correct}/{len(runnable)} = {accuracy:.0%}{note}")
    return accuracy


async def main(args: argparse.Namespace) -> int:
    cases = load_cases()
    recall = await eval_retrieval(cases, rerank=not args.no_rerank)
    ok = recall >= args.min_recall
    if not args.retrieval_only:
        ok &= await eval_routing(cases) >= args.min_routing
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument(
        "--no-rerank", action="store_true", help="Score retrieval without the reranker"
    )
    parser.add_argument("--min-recall", type=float, default=0.8)
    parser.add_argument("--min-routing", type=float, default=0.9)
    sys.exit(asyncio.run(main(parser.parse_args())))
