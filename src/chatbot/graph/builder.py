import logging
import time
from collections.abc import Sequence
from datetime import date
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from chatbot.config import Settings
from chatbot.graph.state import AgentState
from chatbot.memory.short_term import close_dangling_tool_calls, split_for_summary, summarize
from chatbot.prompts import load_prompt

logger = logging.getLogger(__name__)


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


def log_model_call(response: AIMessage, seconds: float) -> None:
    usage = response.usage_metadata
    if response.tool_calls:
        outcome = "tool calls: " + ", ".join(c["name"] for c in response.tool_calls)
    else:
        outcome = f"answer of {len(response.text)} chars"
    logger.info(
        "model replied in %.1fs (%s tokens in, %s out): %s",
        seconds,
        usage["input_tokens"] if usage else "?",
        usage["output_tokens"] if usage else "?",
        outcome,
    )
    if response.response_metadata.get("finish_reason") == "content_filter":
        logger.warning("response blocked by content filter")


def build_graph(
    settings: Settings,
    tools: Sequence[BaseTool] = (),
    llm: BaseChatModel | None = None,
    summarizer: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[AgentState]:
    """Without a checkpointer every call starts a new conversation. `summarizer` defaults to
    SUMMARY_MODEL, or to `llm` when one is injected (tests)."""
    model = llm or build_llm(settings)
    summary_model = summarizer or llm or build_llm(settings, settings.summary_model, False)
    if tools:
        model = model.bind_tools(tools)  # type: ignore[assignment]
    prompt_template = load_prompt("system")

    async def manage_context(state: AgentState) -> dict[str, Any]:
        messages = state["messages"]
        tokens = count_tokens_approximately(messages)
        limit = settings.context_max_tokens
        logger.info("context: %d messages, ~%d/%d tokens", len(messages), tokens, limit)
        cut = split_for_summary(messages, limit, settings.context_keep_turns)
        if not cut:
            return {}
        logger.info("over the limit: summarizing the oldest %d of %d messages", cut, len(messages))
        start = time.monotonic()
        try:
            summary = await summarize(summary_model, state.get("summary", ""), messages[:cut])
        except Exception:
            # Answering with a long history beats not answering; try again next turn.
            logger.exception("summarizing %d messages failed", cut)
            return {}
        logger.info(
            "summary updated in %.1fs (%d chars); thread now ~%d tokens",
            time.monotonic() - start,
            len(summary),
            count_tokens_approximately(messages[cut:]),
        )
        return {
            "summary": summary,
            "messages": [RemoveMessage(id=m.id) for m in messages[:cut] if m.id],
        }

    async def llm_node(state: AgentState) -> dict[str, Any]:
        system_text = prompt_template.replace("{today}", date.today().isoformat())
        if summary := state.get("summary"):
            system_text += (
                "\n\n## Earlier in this conversation\n\n"
                "Older messages of this conversation were condensed into the summary below. "
                "Treat it as what the user and you said earlier, and answer from it when "
                "they ask about it.\n\n"
                f"<conversation_summary>\n{summary}\n</conversation_summary>"
            )
        messages = close_dangling_tool_calls(state["messages"])
        if len(messages) != len(state["messages"]):
            logger.warning("added placeholder results for interrupted tool calls")
        start = time.monotonic()
        response = await model.ainvoke([SystemMessage(system_text), *messages])
        log_model_call(response, time.monotonic() - start)
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("manage_context", manage_context)
    graph.add_node("llm", llm_node)
    graph.add_edge(START, "manage_context")
    graph.add_edge("manage_context", "llm")
    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("llm", tools_condition, {"tools": "tools", END: END})
        graph.add_edge("tools", "llm")
    else:
        graph.add_edge("llm", END)
    return graph.compile(checkpointer=checkpointer)
