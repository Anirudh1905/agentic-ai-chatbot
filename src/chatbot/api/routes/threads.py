import logging
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Response, status
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from chatbot.api.dependencies import Callbacks, Checkpointer, Graph, Threads, UserId
from chatbot.api.routes.chat import collect_sources, run_config
from chatbot.api.schemas import (
    ChatMessage,
    ChatResponse,
    CheckpointInfo,
    CreateThreadRequest,
    ResumeRequest,
    ThreadInfo,
    ThreadMessages,
)
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
    return ThreadMessages(
        thread_id=thread_id,
        summary=state.values.get("summary") or None,
        messages=to_chat_messages(state.values.get("messages", [])),
    )


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


@router.post("/{thread_id}/resume")
async def resume_thread(
    thread_id: UUID,
    graph: Graph,
    threads: Threads,
    user_id: UserId,
    callbacks: Callbacks,
    body: ResumeRequest | None = None,
) -> ChatResponse:
    """Continue a run that stopped before finishing (crash, disconnect, timeout).

    With `checkpoint_id`, replay from that earlier checkpoint instead. The new steps fork
    from it and become the thread's latest state; the old branch stays in the history.
    """
    await owned_thread(thread_id, user_id, threads)
    config = run_config(thread_id, callbacks, body.checkpoint_id if body else None)
    state = await graph.aget_state(config)
    if state.created_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Checkpoint not found")
    if not state.next:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing to resume: the run finished")
    logger.info(
        "resuming from checkpoint %s (step %s, next: %s)",
        state.config["configurable"]["checkpoint_id"],
        state.metadata.get("step") if state.metadata else None,
        ", ".join(state.next),
    )
    result = await graph.ainvoke(None, config=config)
    return ChatResponse(
        reply=result["messages"][-1].text,
        thread_id=thread_id,
        sources=collect_sources(result["messages"]),
    )


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: UUID, threads: Threads, checkpointer: Checkpointer, user_id: UserId
) -> Response:
    """Hard delete: every checkpoint of the thread, then its metadata."""
    await owned_thread(thread_id, user_id, threads)
    # Checkpoints first: if this fails the thread is still listed and the delete can be retried.
    await checkpointer.adelete_thread(str(thread_id))
    await threads.delete(thread_id)
    logger.info("deleted thread and all its checkpoints")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
