"""Translate LangGraph run events into the SSE events defined in schemas.StreamEvent."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from chatbot.api.schemas import StreamEvent
from chatbot.graph.builder import MODEL_NODE
from chatbot.graph.state import AgentState

logger = logging.getLogger(__name__)


def sse(event: StreamEvent, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, default=str)}


def pending_request(state: StateSnapshot) -> dict[str, Any] | None:
    """The approval request a paused thread is waiting on, or None."""
    for pending in state.interrupts:
        if isinstance(pending.value, dict) and "action_requests" in pending.value:
            return pending.value
    return None


async def stream_run(
    graph: CompiledStateGraph[AgentState],
    graph_input: AgentState | Command[Any] | None,
    config: RunnableConfig,
    thread_id: UUID,
) -> AsyncIterator[dict[str, str]]:
    """Run the graph and stream it. If the run pauses for approval, an INTERRUPT event with
    the pending request comes just before DONE."""
    try:
        async for event in graph.astream_events(graph_input, config=config, version="v2"):
            kind = event["event"]
            # Only the agent's own model call is the reply; the summarizer streams too.
            if (
                kind == "on_chat_model_stream"
                and event["metadata"].get("langgraph_node") == MODEL_NODE
            ):
                chunk: AIMessageChunk = event["data"]["chunk"]
                # .text skips reasoning blocks and returns only visible text.
                if text := chunk.text:
                    yield sse(StreamEvent.TOKEN, {"token": text})
            elif kind == "on_tool_start":
                yield sse(
                    StreamEvent.TOOL_START,
                    {
                        "id": event["run_id"],
                        "name": event["name"],
                        "input": event["data"].get("input"),
                    },
                )
            elif kind == "on_tool_end":
                output = event["data"].get("output")
                sources = output.artifact if isinstance(output, ToolMessage) else None
                yield sse(
                    StreamEvent.TOOL_END,
                    {"id": event["run_id"], "name": event["name"], "sources": sources or []},
                )
        pending = pending_request(await graph.aget_state(config))
        if pending:
            yield sse(StreamEvent.INTERRUPT, {"thread_id": thread_id, **pending})
        yield sse(StreamEvent.DONE, {"thread_id": thread_id, "pending": pending is not None})
    except Exception:
        logger.exception("stream failed")
        yield sse(StreamEvent.ERROR, {"message": "Something went wrong. Please try again."})
