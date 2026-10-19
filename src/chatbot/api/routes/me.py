"""The signed-in user's own data: their long-term memories (memory/long_term/), to see and
forget, and deleting their account with everything kept about them."""

import logging
from contextlib import nullcontext

from fastapi import APIRouter, HTTPException, Response, status

from chatbot.api.auth import UserId
from chatbot.api.dependencies import Checkpointer, Store, Threads, Users, Writer
from chatbot.api.schemas import Memory, MemoryList
from chatbot.memory.long_term import delete, forget_user, list_memories, namespace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me")


@router.get("/memory", tags=["memory"])
async def get_memories(store: Store, user_id: UserId) -> MemoryList:
    """What the bot remembers about you across conversations."""
    items = await list_memories(store, user_id)
    return MemoryList(
        memories=[
            Memory(
                id=i.key,
                content=i.value["content"],
                scope=i.value.get("scope", "user"),
                topic=i.value.get("topic", "personal"),
                updated_at=i.updated_at,
            )
            for i in items
        ]
    )


@router.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["memory"])
async def forget_memory(memory_id: str, store: Store, user_id: UserId) -> Response:
    """Forget one memory."""
    if await store.aget(namespace(user_id), memory_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    await delete(store, user_id, memory_id)
    logger.info("user %s deleted a long-term memory", user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT, tags=["memory"])
async def forget_everything(store: Store, user_id: UserId) -> Response:
    """Forget everything: no turn recalls them any more."""
    items = await list_memories(store, user_id)
    for item in items:
        await delete(store, user_id, item.key)
    logger.info("user %s deleted all %d long-term memories", user_id, len(items))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, tags=["account"])
async def delete_me(
    threads: Threads,
    checkpointer: Checkpointer,
    store: Store,
    writer: Writer,
    users: Users,
    user_id: UserId,
) -> Response:
    """Delete my account and my data: every thread with all its checkpoints, my long-term
    memories and memory cursors, then the account itself, so my token stops working.

    A memory push in progress for me finishes first (it would write memories back
    afterwards), and none starts while this runs. Safe to retry: the account goes last.
    """
    lock = writer.user_lock(store, user_id) if writer and store else nullcontext()
    async with lock:
        thread_ids = await threads.ids_for_user(user_id)
        for thread_id in thread_ids:
            await checkpointer.adelete_thread(str(thread_id))
            await threads.delete(thread_id)
        memories = await forget_user(store, user_id) if store else 0
    await users.delete(user_id)
    logger.info(
        "deleted user %s: %d threads, %d long-term memories", user_id, len(thread_ids), memories
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
