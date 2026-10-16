import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from chatbot.agent.approvals import pending_request
from chatbot.agent.builder import Agent
from chatbot.api.dependencies import Callbacks, Graph, Threads, UserId
from chatbot.api.runs import chat_response, redact, run_config, thread_config
from chatbot.api.schemas import ChatRequest, ChatResponse
from chatbot.api.streaming import stream_run
from chatbot.config import RunContext
from chatbot.memory.threads import ThreadStore, make_title
from chatbot.observability import thread_id as thread_id_var

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


async def open_thread(
    body: ChatRequest, user_id: str, threads: ThreadStore, graph: Agent
) -> tuple[UUID, str]:
    """The thread to run in, and the message to send it (redacted)."""
    thread_id = body.thread_id or uuid4()
    thread_id_var.set(str(thread_id))
    message = redact(body.message)
    thread = await threads.claim(thread_id, user_id, make_title(message))
    if thread is None:
        logger.warning("user %s tried to post to a thread they don't own", user_id)
        # 404, not 403: don't confirm that someone else's thread exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    if body.thread_id and pending_request(await graph.aget_state(thread_config(thread_id))):
        # A new message would leave the pending tool calls without results.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This thread is waiting for approval. Answer it with POST /threads/{id}/resume "
            "before sending a new message.",
        )
    logger.info(
        "%s thread, message of %d chars",
        "new" if thread.created_at == thread.updated_at else "continuing",
        len(body.message),
    )
    return thread_id, message


@router.post("")
async def chat(
    body: ChatRequest, graph: Graph, threads: Threads, user_id: UserId, callbacks: Callbacks
) -> ChatResponse:
    thread_id, message = await open_thread(body, user_id, threads, graph)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(message)]},
        config=run_config(thread_id, callbacks),
        context=RunContext(user_id),
    )
    return await chat_response(graph, result, thread_id)


@router.post("/stream")
async def chat_stream(
    body: ChatRequest, graph: Graph, threads: Threads, user_id: UserId, callbacks: Callbacks
) -> EventSourceResponse:
    thread_id, message = await open_thread(body, user_id, threads, graph)
    return EventSourceResponse(
        stream_run(
            graph,
            {"messages": [HumanMessage(message)]},
            run_config(thread_id, callbacks),
            RunContext(user_id),
            thread_id,
        )
    )
