"""The worker: runs the memory pushes the API enqueues, and the background loops (Phase 9).

Usage: python -m chatbot.worker

- **Pushes:** long-polls the queue (SQS, or ElasticMQ standing in for it) and runs each push.
  A push is acked (deleted) only once it succeeded; one that fails comes back after the queue's
  visibility timeout and, after 5 receives, moves to the dead-letter queue. One user's pushes
  run in order (the queue's FIFO group), different users' side by side.
- **The idle-thread scan** (every MEMORY_TTL_CHECK_EVERY_MINUTES), which enqueues a push for
  each thread idle past MEMORY_THREAD_TTL_MINUTES, and the memory store's **TTL sweeper**.
  API pods run neither (BACKGROUND_JOBS=false).
- **Metrics** on WORKER_METRICS_PORT: pushes by outcome, the queue's depth, how long jobs waited.

SIGTERM stops receiving, lets the pushes in hand finish, then exits.
"""

import asyncio
import logging
import signal
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from langchain_core.messages import AnyMessage
from langgraph.store.base import BaseStore
from prometheus_client import start_http_server

from chatbot.accounts.users import PostgresUserStore
from chatbot.agent.builder import build_llm
from chatbot.config import get_settings
from chatbot.jobs import JobQueue, Received, SqsJobQueue
from chatbot.memory.long_term import MemoryWriter
from chatbot.memory.threads import PostgresThreadStore
from chatbot.observability.logs import configure_logging
from chatbot.observability.metrics import QUEUE_DEPTH, QUEUE_JOB_AGE
from chatbot.reliability.breaker import Breakers
from chatbot.storage.db import open_pool
from chatbot.storage.embeddings import build_embeddings
from chatbot.storage.langgraph import build_checkpointer, build_store

logger = logging.getLogger("chatbot.worker")

MessagesOf = Callable[[str], Awaitable[Sequence[AnyMessage]]]

# How often the queue's depth is read for the metrics.
DEPTH_EVERY_SECONDS = 15


class Worker:
    def __init__(
        self, queue: JobQueue, writer: MemoryWriter, store: BaseStore, messages_of: MessagesOf
    ) -> None:
        self.queue = queue
        self.writer = writer
        self.store = store
        self.messages_of = messages_of

    async def run_once(self, wait_seconds: int = 20) -> int:
        """Receive one batch and run it. Returns how many pushes it received."""
        batch = await self.queue.receive(wait_seconds=wait_seconds)
        by_user: dict[str, list[Received]] = defaultdict(list)
        for received in batch:
            if received.sent_at is not None:
                QUEUE_JOB_AGE.observe(max(0.0, time.time() - received.sent_at))
            by_user[received.push.user_id].append(received)

        async def run_user(jobs: list[Received]) -> None:
            for received in jobs:  # in the order the queue gave them
                if await self.writer.run(self.store, received.push, self.messages_of):
                    await self.queue.ack(received)
                else:
                    logger.warning(
                        "%s push failed (receive %d); it comes back after the visibility timeout",
                        received.push.kind,
                        received.receives,
                    )

        await asyncio.gather(*(run_user(jobs) for jobs in by_user.values()))
        return len(batch)

    async def run(self, stop: asyncio.Event) -> None:
        last_depth = 0.0
        while not stop.is_set():
            if time.monotonic() - last_depth > DEPTH_EVERY_SECONDS:
                await self.record_depth()
                last_depth = time.monotonic()
            try:
                await self.run_once()
            except Exception:
                logger.exception("receiving from the queue failed; retrying")
                await asyncio.sleep(5)

    async def record_depth(self) -> None:
        try:
            depth = await self.queue.depth()
        except Exception:
            logger.warning("couldn't read the queue's depth", exc_info=True)
            return
        QUEUE_DEPTH.labels("visible").set(depth.visible)
        QUEUE_DEPTH.labels("in_flight").set(depth.in_flight)
        QUEUE_DEPTH.labels("dead").set(depth.dead)


async def serve() -> None:
    settings = get_settings()
    start_http_server(settings.worker_metrics_port)
    pool = await open_pool(settings)
    checkpointer = build_checkpointer(pool)
    # Shared in Redis with the API pods (reliability/breaker.py).
    breakers = Breakers.shared(settings)
    store = build_store(pool, settings, build_embeddings(settings, breakers))
    queue = SqsJobQueue(settings)
    await queue.open()

    async def messages_of(thread_id: str) -> Sequence[AnyMessage]:
        saved = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        return list(saved.checkpoint["channel_values"].get("messages", [])) if saved else []

    writer = MemoryWriter(
        build_llm(settings, settings.memory_model, streaming=False),
        fallback=build_llm(settings, settings.fallback_model, streaming=False)
        if settings.fallback_model
        else None,
        concurrency=settings.memory_write_concurrency,
        queue=queue,  # the idle scan enqueues; the worker runs what it receives
        users=PostgresUserStore(pool),
    )
    await store.start_ttl_sweeper()
    writer.start(
        store,
        PostgresThreadStore(pool),
        messages_of,
        ttl=timedelta(minutes=settings.memory_thread_ttl_minutes),
        every=timedelta(minutes=settings.memory_ttl_check_every_minutes),
    )
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    logger.info("worker ready: queue %s", settings.sqs_queue_url)
    try:
        await Worker(queue, writer, store, messages_of).run(stop)
    finally:
        logger.info("worker stopping")
        await writer.stop()
        await store.stop_ttl_sweeper(timeout=5)
        await queue.close()
        await pool.close()


def main() -> int:
    configure_logging(get_settings())
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
