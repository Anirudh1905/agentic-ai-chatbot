"""The per-thread run lease (Phase 9): one run at a time per thread, across every API pod.

Two runs on one thread would both read the same checkpoint, and the later write would win
(Phase 3). So a run takes a lease on its thread's row before it starts (refused with 409 if
another run holds it), renews it while it goes, and releases it when it ends. If its pod
dies, nobody renews it, and the thread frees itself when it expires (THREAD_LEASE_SECONDS).

A run paused for approval has ended: it holds no lease, and the resume takes a new one.
"""

import asyncio
import logging
from types import TracebackType
from uuid import UUID, uuid4

import anyio
from fastapi import HTTPException, status

from chatbot.config import get_settings
from chatbot.memory.threads import ThreadStore
from chatbot.observability.metrics import LEASE_RENEWAL_FAILURES, LIMITED

logger = logging.getLogger(__name__)

BUSY = "This chat is still answering your last message. Try again when it has finished."


class ThreadLease:
    """A run's lease on its thread, renewed in the background until released. Use it as an
    async context manager, or call release() (a stream releases it when it ends)."""

    def __init__(self, threads: ThreadStore, thread_id: UUID, seconds: float) -> None:
        self.threads = threads
        self.thread_id = thread_id
        self.seconds = seconds
        self.run_id = uuid4()
        self._renewing: asyncio.Task[None] | None = None

    def start_renewing(self) -> None:
        self._renewing = asyncio.create_task(self._renew())

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self.seconds / 3)
            try:
                held = await self.threads.renew(self.thread_id, self.run_id, self.seconds)
            except Exception:
                # Stopping the reply halfway would be worse than the small chance of a second
                # run starting once the lease expires.
                LEASE_RENEWAL_FAILURES.inc()
                logger.warning("couldn't renew the thread's lease; carrying on", exc_info=True)
                continue
            if not held:
                LEASE_RENEWAL_FAILURES.inc()
                logger.warning("lost the thread's lease (it expired); carrying on without it")
                return

    async def release(self) -> None:
        if self._renewing:
            self._renewing.cancel()
            self._renewing = None
        try:
            # Shielded: a stream the client left is cancelled (sse-starlette), and every await
            # in a cancelled scope would be cancelled too, leaving the lease to expire.
            with anyio.CancelScope(shield=True):
                await self.threads.release(self.thread_id, self.run_id)
        except Exception:
            logger.warning("couldn't release the thread's lease; it expires", exc_info=True)

    async def __aenter__(self) -> "ThreadLease":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()


async def take_lease(threads: ThreadStore, thread_id: UUID, user_id: str) -> ThreadLease:
    """The thread's lease for a new run, or 409 if a run on it is still going. The thread must
    already be the user's (checked by the caller, which answers 404 if not)."""
    lease = ThreadLease(threads, thread_id, get_settings().thread_lease_seconds)
    if not await threads.lease(thread_id, user_id, lease.run_id, lease.seconds):
        LIMITED.labels("thread_busy").inc()
        logger.info("refused: a run on this thread is still going")
        raise HTTPException(status.HTTP_409_CONFLICT, BUSY, headers={"Retry-After": "5"})
    lease.start_renewing()
    return lease
