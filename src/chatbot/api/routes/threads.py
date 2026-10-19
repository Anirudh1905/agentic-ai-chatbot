import logging
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Response, status
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse

from chatbot.agent.approvals import pending_request
from chatbot.agent.builder import Agent
from chatbot.api.auth import BudgetedUserId, UserCredentials, UserId
from chatbot.api.dependencies import (
    Callbacks,
    Checkpointer,
    Graph,
    Store,
    Threads,
    Writer,
)
from chatbot.api.runs import chat_response, redact, run_config
from chatbot.api.schemas import (
    ChatMessage,
    ChatResponse,
    CheckpointInfo,
    CreateThreadRequest,
    ResumeRequest,
    ThreadInfo,
    ThreadMessages,
)
from chatbot.api.streaming import stream_run
from chatbot.config import RunContext
from chatbot.memory.long_term import cursor_namespace
from chatbot.memory.short_term import summary_text
from chatbot.memory.threads import Thread, ThreadStore
from chatbot.observability import thread_id as thread_id_var

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])


async def owned_thread(thread_id: UUID, user_id: str, threads: ThreadStore) -> Thread:
    thread_id_var.set(str(thread_id))
    thread = await threads.get(thread_id, user_id)
    if thread is None:
        logger.warning("user %s asked for a thread that is missing or not theirs", user_id)
        # Same answer for "doesn't exist" and "not yours".
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    return thread


def thread_info(thread: Thread) -> ThreadInfo:
    return ThreadInfo(
        id=thread.id,
        title=thread.title,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def to_chat_messages(messages: list[AnyMessage]) -> list[ChatMessage]:
    """User messages and final replies, with each reply's tool activity folded into it."""
    out: list[ChatMessage] = []
    tools: list[str] = []
    sources: list[dict[str, Any]] = []
    for m in messages:
        if summary_text(m) is not None:
            continue  # shown separately as ThreadMessages.summary
        if isinstance(m, HumanMessage):
            out.append(ChatMessage(role="user", content=m.text))
            tools, sources = [], []
        elif isinstance(m, ToolMessage):
            sources.extend(m.artifact or [])
        elif isinstance(m, AIMessage):
            tools.extend(call["name"] for call in m.tool_calls)
            if m.text:
                out.append(
                    ChatMessage(role="assistant", content=m.text, tools=tools, sources=sources)
                )
                tools, sources = [], []
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_thread(
    threads: Threads, user_id: UserId, body: CreateThreadRequest | None = None
) -> ThreadInfo:
    thread = await threads.create(uuid4(), user_id, body.title if body else None)
    thread_id_var.set(str(thread.id))
    logger.info("created thread")
    return thread_info(thread)


@router.get("")
async def list_threads(
    threads: Threads, user_id: UserId, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ThreadInfo]:
    return [thread_info(t) for t in await threads.list_for_user(user_id, limit)]


@router.get("/{thread_id}/messages")
async def get_messages(
    thread_id: UUID, graph: Graph, threads: Threads, user_id: UserId
) -> ThreadMessages:
    await owned_thread(thread_id, user_id, threads)
    state = await graph.aget_state({"configurable": {"thread_id": str(thread_id)}})
    messages = state.values.get("messages", [])
    return ThreadMessages(
        thread_id=thread_id,
        summary=next(filter(None, map(summary_text, messages)), None),
        messages=to_chat_messages(messages),
        pending=pending_request(state),
    )


@router.post("/{thread_id}/messages/{index}/remember", status_code=status.HTTP_202_ACCEPTED)
async def remember_message(
    thread_id: UUID,
    index: int,
    graph: Graph,
    threads: Threads,
    store: Store,
    writer: Writer,
    user_id: UserId,
) -> Response:
    """Push one of the bot's replies (its position in GET .../messages) into the user's
    long-term memory, in the background, together with the question it answered."""
    await owned_thread(thread_id, user_id, threads)
    if writer is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Memory is turned off")
    state = await graph.aget_state({"configurable": {"thread_id": str(thread_id)}})
    messages = to_chat_messages(state.values.get("messages", []))
    if not 0 <= index < len(messages):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if messages[index].role != "assistant":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only the bot's replies can be remembered"
        )
    question = next((m.content for m in reversed(messages[:index]) if m.role == "user"), None)
    writer.remember(store, user_id, question, messages[index].content)
    logger.info("user asked to remember reply %d", index)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/{thread_id}/history")
async def get_history(
    thread_id: UUID,
    graph: Graph,
    threads: Threads,
    user_id: UserId,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[CheckpointInfo]:
    """Checkpoints newest first, for debugging and time travel (see POST .../resume)."""
    await owned_thread(thread_id, user_id, threads)
    history = []
    config = {"configurable": {"thread_id": str(thread_id)}}
    async for snap in graph.aget_state_history(config, limit=limit):  # type: ignore[arg-type]
        messages = snap.values.get("messages", [])
        parent = snap.parent_config["configurable"]["checkpoint_id"] if snap.parent_config else None
        history.append(
            CheckpointInfo(
                checkpoint_id=snap.config["configurable"]["checkpoint_id"],
                parent_checkpoint_id=parent,
                created_at=snap.created_at,
                source=snap.metadata.get("source") if snap.metadata else None,
                step=snap.metadata.get("step") if snap.metadata else None,
                next=list(snap.next),
                message_count=len(messages),
                last_message=f"{messages[-1].type}: {messages[-1].text[:200]}"
                if messages
                else None,
            )
        )
    return history


async def prepare_resume(
    thread_id: UUID,
    body: ResumeRequest | None,
    graph: Agent,
    callbacks: list[BaseCallbackHandler],
) -> tuple[RunnableConfig, Command[Any] | None]:
    """The config and graph input that continue the thread, or raise if it can't continue."""
    body = body or ResumeRequest()
    config = run_config(thread_id, callbacks, body.checkpoint_id)
    state = await graph.aget_state(config)
    if state.created_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Checkpoint not found")
    if pending := pending_request(state):
        if body.decisions is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The thread is waiting for approval: send one decision per pending tool call",
            )
        # The middleware matches decisions to calls by position; a wrong count would fail
        # halfway through the run.
        if len(body.decisions) != len(pending["action_requests"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Expected {len(pending['action_requests'])} decisions, one per pending "
                f"tool call; got {len(body.decisions)}",
            )
        logger.info(
            "decisions: %s",
            ", ".join(
                f"{a['name']}={d.type}"
                for a, d in zip(pending["action_requests"], body.decisions, strict=True)
            ),
        )
        decisions = [d.model_dump(exclude_none=True) for d in body.decisions]
        for d in decisions:
            # The reviewer's text goes into the thread too, so it's redacted like a message.
            if d["type"] == "edit":
                args = d["edited_action"]["args"]
                d["edited_action"]["args"] = {
                    k: redact(v) if isinstance(v, str) else v for k, v in args.items()
                }
            if d["type"] == "reject" and d.get("message"):
                # The middleware sends "User rejected the tool call ... with reason: <message>".
                # A bare reason like "no" reads as the tool failing; say it was a choice.
                d["message"] = (
                    redact(d["message"])
                    + ". It was not run; don't retry it, and answer without it."
                )
        return config, Command(resume={"decisions": decisions})
    if body.decisions:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing in this thread needs approval")
    if not state.next:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing to resume: the run finished")
    logger.info(
        "resuming from checkpoint %s (step %s, next: %s)",
        state.config["configurable"]["checkpoint_id"],
        state.metadata.get("step") if state.metadata else None,
        ", ".join(state.next),
    )
    return config, None


@router.post("/{thread_id}/resume")
async def resume_thread(
    thread_id: UUID,
    graph: Graph,
    threads: Threads,
    user_id: BudgetedUserId,
    callbacks: Callbacks,
    credentials: UserCredentials,
    body: ResumeRequest | None = None,
) -> ChatResponse:
    """Continue a paused or interrupted run.

    - Waiting for approval: send `decisions`, one per pending tool call, in order.
    - Stopped before finishing (crash, disconnect, timeout): send nothing.
    - With `checkpoint_id`, replay from that earlier checkpoint instead. The new steps fork
      from it and become the thread's latest state; the old branch stays in the history.
    """
    await owned_thread(thread_id, user_id, threads)
    config, graph_input = await prepare_resume(thread_id, body, graph, callbacks)
    result = await graph.ainvoke(
        graph_input, config=config, context=RunContext(user_id, credentials)
    )
    return await chat_response(graph, result, thread_id)


@router.post("/{thread_id}/resume/stream")
async def resume_thread_stream(
    thread_id: UUID,
    graph: Graph,
    threads: Threads,
    user_id: BudgetedUserId,
    callbacks: Callbacks,
    credentials: UserCredentials,
    body: ResumeRequest | None = None,
) -> EventSourceResponse:
    """Same as POST .../resume, streamed like /chat/stream."""
    await owned_thread(thread_id, user_id, threads)
    config, graph_input = await prepare_resume(thread_id, body, graph, callbacks)
    return EventSourceResponse(
        stream_run(graph, graph_input, config, RunContext(user_id, credentials), thread_id)
    )


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: UUID, threads: Threads, checkpointer: Checkpointer, store: Store, user_id: UserId
) -> Response:
    """Hard delete: every checkpoint of the thread, then its metadata. Long-term memories
    already pushed from it stay (DELETE /me/memory clears them)."""
    await owned_thread(thread_id, user_id, threads)
    # Checkpoints first: if this fails the thread is still listed and the delete can be retried.
    await checkpointer.adelete_thread(str(thread_id))
    if store:
        await store.adelete(cursor_namespace(user_id), str(thread_id))
    await threads.delete(thread_id)
    logger.info("deleted thread and all its checkpoints")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
