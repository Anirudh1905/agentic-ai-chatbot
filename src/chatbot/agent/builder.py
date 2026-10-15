from collections.abc import Sequence
from typing import Any, cast

from langchain.agents import AgentState as _AgentState
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from chatbot.agent.middleware import ModelCallLimit, ModelCallMiddleware, system_prompt
from chatbot.config import Settings
from chatbot.memory.long_term import LongTermMemory, MemoryWriter
from chatbot.memory.short_term import summarization
from chatbot.pii import output_middleware
from chatbot.tools.registry import tool_retry

# create_agent's state: the messages, plus a few internal keys its middleware uses.
AgentState = _AgentState[Any]

# Graph steps per run, passed in every run's config. ModelCallLimitMiddleware is the real cap
# (MAX_MODEL_CALLS); this is a backstop. Each model/tool round is many steps, one per
# middleware hook (the four PIIMiddleware instances alone add eight), so LangGraph's default
# of 25 would stop a run after two rounds. Setting it with agent.with_config() doesn't work:
# a run's own config replaces it.
RECURSION_LIMIT = 200

# The node that calls the chat model in create_agent's graph. Its tokens are the reply.
MODEL_NODE = "model"


def build_llm(settings: Settings, model: str | None = None, streaming: bool = True) -> ChatOpenAI:
    return ChatOpenAI(
        model=model or settings.model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        reasoning_effort=settings.reasoning_effort,
        max_completion_tokens=settings.max_tokens,
        timeout=settings.request_timeout,
        streaming=streaming,
        # Token counts on streamed responses too (for logs and tracing).
        stream_usage=True,
    )


def build_graph(
    settings: Settings,
    tools: Sequence[BaseTool] = (),
    llm: BaseChatModel | None = None,
    summarizer: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    store: BaseStore | None = None,
    memory_model: BaseChatModel | None = None,
) -> CompiledStateGraph[AgentState]:
    """The agent: LangChain's create_agent loop (model ↔ tools) plus middleware.

    Without a checkpointer every call starts a new conversation. `summarizer` defaults to
    SUMMARY_MODEL, or to `llm` when one is injected (tests). With `interrupt_on` (see
    agent/approvals.py), matching tool calls pause for a human; this needs a checkpointer. With
    a `store` (and MEMORY_ENABLED), the agent keeps long-term memories of each user
    (memory/long_term.py), written by `memory_model` (default MEMORY_MODEL).
    """
    model = llm or build_llm(settings)
    summary_model = summarizer or llm or build_llm(settings, settings.summary_model, False)
    writer = None
    if store is not None and settings.memory_enabled:
        writer = MemoryWriter(memory_model or build_llm(settings, settings.memory_model, False))
    middleware: list[AgentMiddleware[Any, Any]] = [
        # Recalls the user's long-term memories when a thread starts.
        *([LongTermMemory(settings.memory_recall_limit)] if writer else []),
        # PII in tool results and replies (user messages are redacted earlier, by the API).
        *(output_middleware() if settings.pii_enabled else []),
        summarization(settings, summary_model, on_summary=writer.schedule if writer else None),
        system_prompt,
        ModelCallMiddleware(),
        # Ends the run with a short message rather than an error if the model keeps going.
        ModelCallLimit(run_limit=settings.max_model_calls, exit_behavior="end"),
    ]
    if retry := tool_retry(tools):
        middleware.append(retry)
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on))
    agent = create_agent(
        model, tools=list(tools), middleware=middleware, checkpointer=checkpointer, store=store
    )
    return cast(CompiledStateGraph[AgentState], agent)
