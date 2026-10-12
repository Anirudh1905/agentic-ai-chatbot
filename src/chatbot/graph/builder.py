import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from chatbot.config import Settings
from chatbot.graph.state import AgentState
from chatbot.prompts import load_prompt

logger = logging.getLogger(__name__)


def build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        reasoning_effort=settings.reasoning_effort,
        max_completion_tokens=settings.max_tokens,
        timeout=settings.request_timeout,
        streaming=True,
    )


def build_graph(
    settings: Settings, tools: Sequence[BaseTool] = (), llm: BaseChatModel | None = None
) -> CompiledStateGraph[AgentState]:
    model = llm or build_llm(settings)
    if tools:
        model = model.bind_tools(tools)  # type: ignore[assignment]
    prompt_template = load_prompt("system")

    async def llm_node(state: AgentState) -> dict[str, Any]:
        system_prompt = SystemMessage(prompt_template.replace("{today}", date.today().isoformat()))
        response = await model.ainvoke([system_prompt, *state["messages"]])
        if response.response_metadata.get("finish_reason") == "content_filter":
            logger.warning("response blocked by content filter")
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("llm", llm_node)
    graph.add_edge(START, "llm")
    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("llm", tools_condition, {"tools": "tools", END: END})
        graph.add_edge("tools", "llm")
    else:
        graph.add_edge("llm", END)
    return graph.compile()
