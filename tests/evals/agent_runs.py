"""One agent run per question: its answer, the tools it called and the results they returned.

Routing, faithfulness, citations and correctness are all scored from the same run. The graph
has no checkpointer and no approval rules, so every question is a new conversation and searches
run straight through. With the LANGFUSE keys set, each run is traced (tagged "eval"), and its
scores are sent to its trace, so a failed case links to the run behind it.
"""

import asyncio
import html
import re
from dataclasses import dataclass
from types import TracebackType

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chatbot.agent.builder import RECURSION_LIMIT, build_graph
from chatbot.config import Settings
from chatbot.observability.tracing import build_callbacks, langfuse_client
from chatbot.storage.db import Pool, open_pool
from chatbot.storage.embeddings import build_embeddings
from chatbot.tools.registry import build_tools
from chatbot.tools.rerank import build_reranker
from evals.scoring import Outcome

# The gateway stalls gpt-5 calls when too many are in flight (Phase 6 measurements).
CONCURRENCY = 3

# Both search tools format each result as <result cite_as="...">text</result>.
RESULT = re.compile(r'<result cite_as="([^"]*)">\n(.*?)\n</result>', re.S)


@dataclass(frozen=True)
class Result:
    """One search result the model saw."""

    tool: str
    cite_as: str
    content: str


@dataclass(frozen=True)
class Run:
    question: str
    answer: str
    tools: list[str]  # in the order called, repeats included
    results: list[Result]
    trace_id: str | None = None  # in Langfuse, when tracing is on


def results_of(message: ToolMessage) -> list[Result]:
    return [
        Result(message.name or "", html.unescape(cite), text.strip())
        for cite, text in RESULT.findall(message.text)
    ]


class Agent:
    """The real agent, with the real tools, against the local stack."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.slots = asyncio.Semaphore(CONCURRENCY)
        self.pool: Pool | None = None

    async def __aenter__(self) -> "Agent":
        self.pool = await open_pool(self.settings)
        tools = build_tools(
            self.settings,
            self.pool,
            build_embeddings(self.settings),
            build_reranker(self.settings),
        )
        self.available = {t.name for t in tools}
        self.graph = build_graph(self.settings, tools=tools)
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if langfuse := langfuse_client(self.settings):
            langfuse.flush()  # the traces and scores still buffered
        if self.pool:
            await self.pool.close()

    async def run(self, question: str) -> Run:
        langfuse = langfuse_client(self.settings)
        trace_id = langfuse.create_trace_id() if langfuse else None
        async with self.slots:
            result = await self.graph.ainvoke(
                {"messages": [HumanMessage(question)]},
                config={
                    "recursion_limit": RECURSION_LIMIT,
                    "callbacks": build_callbacks(self.settings, trace_id),
                    "metadata": {
                        "langfuse_trace_name": "eval",
                        "langfuse_tags": ["eval"],
                        "question": question,
                    },
                },
            )
        messages = result["messages"]
        answers = [m for m in messages if isinstance(m, AIMessage) and not m.tool_calls]
        return Run(
            question,
            answers[-1].text if answers else "",
            [tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls],
            [r for m in messages if isinstance(m, ToolMessage) for r in results_of(m)],
            trace_id,
        )

    def send_scores(self, run: Run, outcomes: dict[str, Outcome]) -> None:
        """Each metric's verdict on the run's trace: 1 passed, 0 failed, with the reason."""
        langfuse = langfuse_client(self.settings)
        if langfuse is None or run.trace_id is None:
            return
        for metric, outcome in outcomes.items():
            if outcome.passed is not None:  # an error isn't a verdict
                langfuse.create_score(
                    trace_id=run.trace_id,
                    name=metric,
                    value=float(outcome.passed),
                    data_type="BOOLEAN",
                    comment=outcome.reason[:500] or None,
                )
