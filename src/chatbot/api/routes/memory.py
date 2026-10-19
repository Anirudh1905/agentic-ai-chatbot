"""The user's long-term memories (memory/long_term/): see them, and forget one or all."""

import logging

from fastapi import APIRouter, HTTPException, Response, status

from chatbot.api.auth import UserId
from chatbot.api.dependencies import Store
from chatbot.api.schemas import Memory, MemoryList
from chatbot.memory.long_term import delete, list_memories, namespace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me/memory", tags=["memory"])


@router.get("")
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


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_memory(memory_id: str, store: Store, user_id: UserId) -> Response:
    """Forget one memory."""
    if await store.aget(namespace(user_id), memory_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    await delete(store, user_id, memory_id)
    logger.info("user %s deleted a long-term memory", user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def forget_everything(store: Store, user_id: UserId) -> Response:
    """Forget everything: no turn recalls them any more."""
    items = await list_memories(store, user_id)
    for item in items:
        await delete(store, user_id, item.key)
    logger.info("user %s deleted all %d long-term memories", user_id, len(items))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
