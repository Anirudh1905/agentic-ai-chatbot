"""Deleting a user's account and everything kept about them, in one call."""

import logging
from contextlib import nullcontext

from fastapi import APIRouter, Response, status

from chatbot.api.auth import UserId
from chatbot.api.dependencies import Checkpointer, Store, Threads, Users, Writer
from chatbot.memory.long_term import forget_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["account"])


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
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
