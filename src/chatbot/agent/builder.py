from collections.abc import Sequence
from typing import Any, cast

import httpx
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

from chatbot.accounts.limits import SpendTokens, UsageLimits
from chatbot.agent.middleware import (
    ModelCallLimit,
    ModelCallMiddleware,
    ToolCallMetrics,
    UserModel,
    system_prompt,
)
from chatbot.config import ModelCredentials, RunContext, Settings
from chatbot.guardrails.pii import output_middleware
from chatbot.memory.long_term import LongTermMemory, MemoryWriter
from chatbot.memory.short_term import summarization
from chatbot.tools.memory import build_remember_tool
from chatbot.tools.registry import tool_retry

# create_agent's state: the messages, plus a few internal keys its middleware uses.
AgentState = _AgentState[Any]
# The compiled agent: create_agent's state, and the user each run is for (its runtime context).
Agent = CompiledStateGraph[AgentState, RunContext]

# Graph steps per run, passed in every run's config. ModelCallLimitMiddleware is the real cap
# (MAX_MODEL_CALLS); this is a backstop. Each model/tool round is many steps, one per
# middleware hook (the four PIIMiddleware instances alone add eight), so LangGraph's default
# of 25 would stop a run after two rounds. Setting it with agent.with_config() doesn't work:
# a run's own config replaces it.
RECURSION_LIMIT = 200

# The node that calls the chat model in create_agent's graph. Its tokens are the reply.
MODEL_NODE = "model"


def build_llm(
    settings: Settings,
    model: str | None = None,
    streaming: bool = True,
    timeout: float | None = None,
    credentials: ModelCredentials | None = None,
) -> ChatOpenAI:
    """A chat model on the configured endpoint, or on a user's own (`credentials`). `timeout`
    defaults to REQUEST_TIMEOUT."""
    model = model or settings.model
    return ChatOpenAI(
        model=model,
        api_key=credentials.api_key if credentials else settings.openai_api_key,
        base_url=credentials.base_url if credentials else settings.openai_base_url or None,
        reasoning_effort=settings.reasoning_effort if is_reasoning(model) else None,
        max_completion_tokens=settings.max_tokens,
        timeout=timeout or settings.request_timeout,
        streaming=streaming,
        # Token counts on streamed responses too (for logs and tracing).
        stream_usage=True,
        # The client sends the key as a Bearer token; this adds it the Azure way too.
        default_headers=key_headers(credentials, bearer=False) if credentials else None,
    )


def is_reasoning(model: str) -> bool:
    """Reasoning models take `reasoning_effort`; others (gpt-4.x) reject the parameter."""
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def key_headers(credentials: ModelCredentials, bearer: bool = True) -> dict[str, str]:
    """A user's key both as `Authorization: Bearer` (OpenAI) and as `api-key` (Azure OpenAI,
    and gateways such as ours, which refuse some keys as Bearer). Each endpoint reads the one it
    uses; the key only goes to the endpoint it was given for."""
    key = credentials.api_key.get_secret_value()
    return {"api-key": key} | ({"Authorization": f"Bearer {key}"} if bearer else {})


# The base URL used when a user gives only an API key.
OPENAI_BASE_URL = "https://api.openai.com/v1"


async def check_credentials(settings: Settings, credentials: ModelCredentials) -> str | None:
    """None if `credentials` can run the chat model (MODEL); otherwise what's wrong.

    Two calls: GET /models (the endpoint is OpenAI-compatible and has the model), then a chat
    completion of at most 16 tokens, since some gateways list models without checking the key.
    """
    base = credentials.base_url.rstrip("/")
    headers = key_headers(credentials)
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            listed = await client.get(f"{base}/models", headers=headers)
            if listed.status_code in (401, 403):
                return "The API key was refused"
            if listed.status_code != 200:
                status = listed.status_code
                return f"{credentials.base_url} isn't an OpenAI-compatible API (/models: {status})"
            try:
                body = listed.json()
            except ValueError:
                return f"{credentials.base_url} isn't an OpenAI-compatible API"
            # OpenAI: {"data": [{"id": ...}]}; some gateways: a list of {"value": ...}.
            items = body.get("data", []) if isinstance(body, dict) else body
            models = {m.get("id") or m.get("value") for m in items if isinstance(m, dict)}
            if settings.model not in models:
                return f"The model {settings.model} isn't available there"
            ping: dict[str, Any] = {
                "model": settings.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_completion_tokens": 16,
            }
            if is_reasoning(settings.model):
                ping["reasoning_effort"] = "low"
            answered = await client.post(f"{base}/chat/completions", headers=headers, json=ping)
    except httpx.HTTPError:
        return f"Can't reach {credentials.base_url}"
    if answered.status_code == 200:
        return None
    try:
        reason = answered.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        reason = f"status {answered.status_code}"
    if answered.status_code in (400, 401, 403):  # the gateway answers 400 for a bad key
        return f"The API key was refused ({reason})"
    return f"The model couldn't be called ({reason})"


def build_graph(
    settings: Settings,
    tools: Sequence[BaseTool] = (),
    llm: BaseChatModel | None = None,
    summarizer: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    store: BaseStore | None = None,
    memory_writer: MemoryWriter | None = None,
    usage: UsageLimits | None = None,
) -> Agent:
    """The agent: LangChain's create_agent loop (model ↔ tools) plus middleware.

    Without a checkpointer every call starts a new conversation. `summarizer` defaults to
    SUMMARY_MODEL, or to `llm` when one is injected (tests). With `interrupt_on` (see
    agent/approvals.py), matching tool calls pause for a human; this needs a checkpointer. With
    a `store` (and MEMORY_ENABLED), every turn recalls the user's long-term memories
    (memory/long_term/); with a `memory_writer` too, the turns a summary folds away are
    pushed into them. With `usage` (chatbot/accounts/limits.py), every model call's tokens
    count against the user's daily budget.
    """
    model = llm or build_llm(settings)
    summary_model = summarizer or llm or build_llm(settings, settings.summary_model, False)
    memory = store is not None and settings.memory_enabled
    writer = memory_writer if memory else None
    if writer:
        # Lets the agent save what the user asks it to remember (tools/memory.py). It's not in
        # `interrupt_on`, so it never waits for approval: it only writes the user's own memories.
        tools = [*tools, build_remember_tool(writer)]
    middleware: list[AgentMiddleware[Any, Any]] = [
        ToolCallMetrics([t.name for t in tools]),
        # Every turn: recall the user's long-term memories closest to their latest message.
        *([LongTermMemory(settings.memory_recall_limit)] if memory else []),
        # PII in tool results and replies (user messages are redacted earlier, by the API).
        *(output_middleware() if settings.pii_enabled else []),
        summarization(settings, summary_model, on_summarize=writer.schedule if writer else None),
        system_prompt,
        ModelCallMiddleware(),
        # A user who logged in with their own endpoint and key: their runs' model calls use it.
        UserModel(settings),
        # Ends the run with a short message rather than an error if the model keeps going.
        ModelCallLimit(run_limit=settings.max_model_calls, exit_behavior="end"),
        *([SpendTokens(usage)] if usage else []),
    ]
    if retry := tool_retry(tools):
        middleware.append(retry)
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on))
    agent = create_agent(
        model,
        tools=list(tools),
        middleware=middleware,
        context_schema=RunContext,
        checkpointer=checkpointer,
        store=store,
    )
    return cast(Agent, agent)
