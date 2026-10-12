"""Translate LangGraph run events into the SSE events defined in schemas.StreamEvent."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from chatbot.api.schemas import StreamEvent
from chatbot.graph.state import AgentState

logger = logging.getLogger(__name__)


def sse(event: StreamEvent, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, default=str)}


async def stream_run(
    graph: CompiledStateGraph[AgentState], graph_input: AgentState, config: RunnableConfig
) -> AsyncIterator[dict[str, str]]:
    try:
        async for event in graph.astream_events(graph_input, config=config, version="v2"):
            kind = event["event"]
            if kind == "on_chat_model_stream":
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
        yield sse(StreamEvent.DONE, {})
    except Exception:
        logger.exception("stream failed")
        yield sse(StreamEvent.ERROR, {"message": "Something went wrong. Please try again."})
