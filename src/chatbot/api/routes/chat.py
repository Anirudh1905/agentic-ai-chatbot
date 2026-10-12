import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from sse_starlette.sse import EventSourceResponse

from chatbot.api.dependencies import Callbacks, Graph, Threads, UserId
from chatbot.api.schemas import ChatRequest, ChatResponse
from chatbot.api.streaming import stream_run
from chatbot.config import get_settings
from chatbot.memory.threads import ThreadStore, make_title
from chatbot.observability import thread_id as thread_id_var

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def run_config(
    thread_id: UUID, callbacks: list[BaseCallbackHandler], checkpoint_id: str | None = None
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": str(thread_id)}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {
        "configurable": configurable,
        "callbacks": callbacks,
        "recursion_limit": get_settings().max_agent_steps,
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


async def open_thread(body: ChatRequest, user_id: str, threads: ThreadStore) -> UUID:
    thread_id = body.thread_id or uuid4()
    thread_id_var.set(str(thread_id))
    thread = await threads.claim(thread_id, user_id, make_title(body.message))
    if thread is None:
        logger.warning("user %s tried to post to a thread they don't own", user_id)
        # 404, not 403: don't confirm that someone else's thread exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    logger.info(
        "%s thread, message of %d chars",
        "new" if thread.created_at == thread.updated_at else "continuing",
        len(body.message),
    )
    return thread_id


@router.post("")
async def chat(
    body: ChatRequest, graph: Graph, threads: Threads, user_id: UserId, callbacks: Callbacks
) -> ChatResponse:
    thread_id = await open_thread(body, user_id, threads)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(body.message)]}, config=run_config(thread_id, callbacks)
    )
    return ChatResponse(
        reply=result["messages"][-1].text,
        thread_id=thread_id,
        sources=collect_sources(result["messages"]),
    )


@router.post("/stream")
async def chat_stream(
    body: ChatRequest, graph: Graph, threads: Threads, user_id: UserId, callbacks: Callbacks
) -> EventSourceResponse:
    thread_id = await open_thread(body, user_id, threads)
    return EventSourceResponse(
        stream_run(
            graph,
            {"messages": [HumanMessage(body.message)]},
            run_config(thread_id, callbacks),
            thread_id,
        )
    )
