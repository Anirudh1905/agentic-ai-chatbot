"""Translate LangGraph run events into the SSE events defined in schemas.StreamEvent."""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from chatbot.agent.approvals import pending_request
from chatbot.agent.builder import MODEL_NODE, Agent, AgentState
from chatbot.agent.middleware import UserModelError
from chatbot.api.leases import ThreadLease
from chatbot.api.schemas import StreamEvent
from chatbot.config import RunContext, get_settings
from chatbot.guardrails.pii import StreamRedactor
from chatbot.observability.metrics import (
    ACTIVE_STREAMS,
    APPROVAL_PAUSES,
    FIRST_TOKEN_SECONDS,
    RUN_ERRORS,
)

logger = logging.getLogger(__name__)


def sse(event: StreamEvent, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, default=str)}


async def stream_run(
    graph: Agent,
    graph_input: AgentState | Command[Any] | None,
    config: RunnableConfig,
    context: RunContext,
    thread_id: UUID,
    lease: ThreadLease,
) -> AsyncIterator[dict[str, str]]:
    """Run the graph and stream it, then release the run's lease on the thread. If the run
    pauses for approval, an INTERRUPT event with the pending request comes just before DONE. If
    a model call fails partway through a reply, RESET comes before the next call's tokens."""
    # PIIMiddleware rewrites the stored reply only after the model has finished, so the
    # tokens the user watches arrive need redacting on the way out too.
    redactor = StreamRedactor() if get_settings().pii_enabled else None
    start, first_token = time.monotonic(), True
    # Whether a model call is under way (started, not ended), and sent tokens.
    in_call = streamed = False

    def token(text: str) -> dict[str, str]:
        nonlocal first_token
        if first_token:
            FIRST_TOKEN_SECONDS.observe(time.monotonic() - start)
            first_token = False
        return sse(StreamEvent.TOKEN, {"token": text})

    ACTIVE_STREAMS.inc()
    try:
        async for event in graph.astream_events(
            graph_input, config=config, context=context, version="v2"
        ):
            kind = event["event"]
            # Only the agent's own model call is the reply; the summarizer streams too.
            from_model = event["metadata"].get("langgraph_node") == MODEL_NODE
            if kind == "on_chat_model_start" and from_model:
                if in_call and streamed:
                    # The call before failed partway (ModelRetryMiddleware retries it, or
                    # ModelFallbackMiddleware asks the fallback model): its reply starts over.
                    yield sse(StreamEvent.RESET, {})
                if redactor:
                    redactor = StreamRedactor()  # anything it held back was the failed reply's
                in_call, streamed = True, False
            elif kind == "on_chat_model_stream" and from_model:
                chunk: AIMessageChunk = event["data"]["chunk"]
                # .text skips reasoning blocks and returns only visible text.
                text = redactor.feed(chunk.text) if redactor else chunk.text
                if text:
                    streamed = True
                    yield token(text)
            elif kind == "on_chat_model_end" and from_model:
                in_call = False
                if redactor and (text := redactor.flush()):
                    yield token(text)
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
            APPROVAL_PAUSES.inc()
            yield sse(StreamEvent.INTERRUPT, {"thread_id": thread_id, **pending})
        yield sse(StreamEvent.DONE, {"thread_id": thread_id, "pending": pending is not None})
    except UserModelError as e:
        RUN_ERRORS.labels("user_model").inc()
        yield sse(StreamEvent.ERROR, {"message": str(e)})
    except Exception:
        RUN_ERRORS.labels("internal").inc()
        logger.exception("stream failed")
        yield sse(StreamEvent.ERROR, {"message": "Something went wrong. Please try again."})
    finally:
        ACTIVE_STREAMS.dec()
        await lease.release()
