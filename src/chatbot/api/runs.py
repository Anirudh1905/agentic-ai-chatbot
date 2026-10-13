"""Shared by the chat and thread routes: run config for the agent, and turning a finished run
into a response."""

import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from chatbot.agent.approvals import pending_request
from chatbot.agent.builder import AgentState
from chatbot.api.schemas import ChatResponse

logger = logging.getLogger(__name__)


def run_config(
    thread_id: UUID, callbacks: list[BaseCallbackHandler], checkpoint_id: str | None = None
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": str(thread_id)}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {
        "configurable": configurable,
        "callbacks": callbacks,
    }


def current_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Messages after the last user message: this turn's tool calls, results and reply."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i + 1 :]
    return messages


def collect_sources(messages: list[AnyMessage]) -> list[dict[str, Any]]:
    return [
        source
        for m in current_turn(messages)
        if isinstance(m, ToolMessage) and m.artifact
        for source in m.artifact
    ]


def thread_config(thread_id: UUID) -> RunnableConfig:
    return {"configurable": {"thread_id": str(thread_id)}}


async def chat_response(
    graph: CompiledStateGraph[AgentState], result: dict[str, Any], thread_id: UUID
) -> ChatResponse:
    pending = pending_request(await graph.aget_state(thread_config(thread_id)))
    if pending:
        logger.info(
            "paused for approval: %s",
            "; ".join(f"{a['name']}({a['args']})" for a in pending["action_requests"]),
        )
    return ChatResponse(
        # While paused, the last message is the model's tool call, not an answer.
        reply="" if pending else result["messages"][-1].text,
        thread_id=thread_id,
        sources=collect_sources(result["messages"]),
        pending=pending,
    )
