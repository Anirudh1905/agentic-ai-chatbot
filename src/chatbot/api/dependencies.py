import asyncio
import logging
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Request
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AnyMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.base import BaseStore
from langgraph.store.postgres.aio import AsyncPostgresStore

from chatbot.accounts.limits import UsageLimits
from chatbot.accounts.users import PostgresUserStore, UserStore
from chatbot.agent.approvals import interrupt_on
from chatbot.agent.builder import Agent, build_graph, build_llm
from chatbot.config import get_settings
from chatbot.jobs import SqsJobQueue
from chatbot.memory.long_term import MemoryWriter
from chatbot.memory.threads import PostgresThreadStore, ThreadStore
from chatbot.observability.tracing import build_callbacks
from chatbot.reliability.breaker import Breakers
from chatbot.storage.db import Pool, open_pool
from chatbot.storage.embeddings import build_embeddings
from chatbot.storage.langgraph import build_checkpointer, build_store
from chatbot.tools.registry import build_tools
from chatbot.tools.rerank import build_reranker
from chatbot.tools.web_search import build_search_cache

logger = logging.getLogger(__name__)


class Resources:
    """Graph, checkpointer and DB pool, created on first request so the app starts even if
    Postgres is down."""

    def __init__(self) -> None:
        self.pool: Pool | None = None
        self.graph: Agent | None = None
        self.checkpointer: AsyncPostgresSaver | None = None
        self.threads: PostgresThreadStore | None = None
        self.store: AsyncPostgresStore | None = None
        self.writer: MemoryWriter | None = None
        self.queue: SqsJobQueue | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        async with self._lock:
            if self.graph is not None:
                return
            settings = get_settings()
            if self.pool is None:
                self.pool = await open_pool(settings)
            # Their tables come from `python -m chatbot.migrate`, run before this process.
            checkpointer = build_checkpointer(self.pool)
            # Shared in Redis with the other pods and the worker (reliability/breaker.py).
            breakers = Breakers.shared(settings)
            embeddings = build_embeddings(settings, breakers)
            store = build_store(self.pool, settings, embeddings)
            if settings.background_jobs:
                await store.start_ttl_sweeper()
            reranker = build_reranker(settings)
            if reranker:
                # Load the model now rather than during the first search.
                await asyncio.to_thread(reranker.load)
            tools = build_tools(
                settings, self.pool, embeddings, reranker, breakers, build_search_cache(settings)
            )
            self.checkpointer = checkpointer
            self.threads = PostgresThreadStore(self.pool)
            self.store = store
            if settings.memory_enabled:
                if settings.sqs_queue_url and self.queue is None:
                    # Pushes go to the worker (Phase 9); without a queue they run here.
                    self.queue = SqsJobQueue(settings)
                    await self.queue.open()
                self.writer = MemoryWriter(
                    # REQUEST_TIMEOUT, though nothing waits on a push: a call takes 3-6 s, and a
                    # stalled gateway call is better retried soon (at 120 s, one push took 6 min).
                    build_llm(settings, settings.memory_model, streaming=False),
                    fallback=build_llm(settings, settings.fallback_model, streaming=False)
                    if settings.fallback_model
                    else None,
                    concurrency=settings.memory_write_concurrency,
                    queue=self.queue,
                )
            self.graph = build_graph(
                settings,
                tools=tools,
                checkpointer=checkpointer,
                store=store,
                memory_writer=self.writer,
                usage=get_limits(),
                breakers=breakers,
                interrupt_on=interrupt_on(tools, settings.hitl_sensitive_terms)
                if settings.hitl_enabled
                else None,
            )
            if self.writer is not None and settings.background_jobs:
                graph = self.graph

                async def messages_of(thread_id: str) -> list[AnyMessage]:
                    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
                    return list(state.values.get("messages", []))

                # Pushes threads whose TTL ran out into long-term memory (memory/long_term/).
                self.writer.start(
                    store,
                    self.threads,
                    messages_of,
                    ttl=timedelta(minutes=settings.memory_thread_ttl_minutes),
                    every=timedelta(minutes=settings.memory_ttl_check_every_minutes),
                )
            logger.info(
                "agent ready: model=%s, summary_model=%s, tools=[%s], context limit %d tokens, "
                "approvals %s, memory %s (%s)",
                settings.model,
                settings.summary_model,
                ", ".join(t.name for t in tools),
                settings.context_max_tokens,
                "on" if settings.hitl_enabled else "off",
                "on" if settings.memory_enabled else "off",
                settings.memory_model,
            )

    async def close(self) -> None:
        if self.store is not None:
            await self.store.stop_ttl_sweeper(timeout=5)
        if self.writer is not None:
            try:
                # Pushes run in the background; give them a moment before the pool goes.
                # One that doesn't finish leaves its thread's cursor, so it runs again later.
                await asyncio.wait_for(self.writer.stop(), timeout=30)
            except TimeoutError:
                logger.warning("shutting down with long-term memory pushes still running")
        if self.queue is not None:
            await self.queue.close()
        if self.pool is not None:
            await self.pool.close()


resources = Resources()


async def get_graph() -> Agent:
    await resources.ensure()
    assert resources.graph is not None
    return resources.graph


async def get_checkpointer() -> BaseCheckpointSaver[Any]:
    await resources.ensure()
    assert resources.checkpointer is not None
    return resources.checkpointer


async def get_store() -> BaseStore:
    await resources.ensure()
    assert resources.store is not None
    return resources.store


async def get_writer() -> MemoryWriter | None:
    await resources.ensure()
    return resources.writer


async def get_threads() -> ThreadStore:
    await resources.ensure()
    assert resources.threads is not None
    return resources.threads


async def get_readiness() -> bool:
    """Whether this process can serve: its resources are up and the database answers. The
    readiness probe; a pod that loses Postgres leaves the Service instead of being restarted
    (that's /healthz, the liveness probe)."""
    try:
        await resources.ensure()
        assert resources.pool is not None
        async with resources.pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        logger.warning("not ready", exc_info=True)
        return False
    return True


async def get_users() -> UserStore:
    await resources.ensure()
    assert resources.pool is not None
    return PostgresUserStore(resources.pool)


@lru_cache
def get_limits() -> UsageLimits | None:
    settings = get_settings()
    return UsageLimits(settings) if settings.rate_limits_enabled else None


Limits = Annotated[UsageLimits | None, Depends(get_limits)]


async def limit_ip(request: Request, limits: Limits) -> None:
    """Per client IP, before authentication, so a flood of bad tokens is limited too. Behind a
    proxy, run uvicorn with --proxy-headers so this is the client's address."""
    if limits and request.client:
        await limits.limit_ip(request.client.host)


async def limit_login(request: Request, limits: Limits) -> None:
    """Register and log-in attempts per client IP, against password guessing."""
    if limits and request.client:
        await limits.limit_login(request.client.host)


@lru_cache
def get_callbacks() -> list[BaseCallbackHandler]:
    return build_callbacks(get_settings())


Graph = Annotated[Agent, Depends(get_graph)]
Checkpointer = Annotated[BaseCheckpointSaver[Any], Depends(get_checkpointer)]
Threads = Annotated[ThreadStore, Depends(get_threads)]
Users = Annotated[UserStore, Depends(get_users)]
Store = Annotated[BaseStore, Depends(get_store)]
Writer = Annotated[MemoryWriter | None, Depends(get_writer)]
Callbacks = Annotated[list[BaseCallbackHandler], Depends(get_callbacks)]
