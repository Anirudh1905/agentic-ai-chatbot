from collections.abc import Sequence
from typing import Any, cast

from langchain.agents import AgentState as _AgentState
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    ModelCallLimitMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from chatbot.agent.middleware import ModelCallMiddleware, summarization, system_prompt
from chatbot.config import Settings
from chatbot.tools.registry import tool_retry

# create_agent's state: the messages, plus a few internal keys its middleware uses.
AgentState = _AgentState[Any]

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
) -> CompiledStateGraph[AgentState]:
    """The agent: LangChain's create_agent loop (model ↔ tools) plus middleware.

    Without a checkpointer every call starts a new conversation. `summarizer` defaults to
    SUMMARY_MODEL, or to `llm` when one is injected (tests). With `interrupt_on` (see
    hitl/policy.py), matching tool calls pause for a human; this needs a checkpointer.
    """
    model = llm or build_llm(settings)
    summary_model = summarizer or llm or build_llm(settings, settings.summary_model, False)
    middleware: list[AgentMiddleware[Any, Any]] = [
        summarization(settings, summary_model),
        system_prompt,
        ModelCallMiddleware(),
        # Ends the run with a short message rather than an error if the model keeps going.
        ModelCallLimitMiddleware(run_limit=settings.max_model_calls, exit_behavior="end"),
    ]
    if retry := tool_retry(tools):
        middleware.append(retry)
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on))
    agent = create_agent(model, tools=list(tools), middleware=middleware, checkpointer=checkpointer)
    # ModelCallLimitMiddleware is the real cap. Each round trip is several graph steps (one
    # per middleware hook), so LangGraph's default limit of 25 would cut in first.
    return cast(CompiledStateGraph[AgentState], agent.with_config(recursion_limit=100))
