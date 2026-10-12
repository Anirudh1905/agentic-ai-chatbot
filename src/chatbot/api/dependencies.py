import asyncio
import logging
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph

from chatbot.config import get_settings
from chatbot.db import Pool, open_pool
from chatbot.embeddings import build_embeddings
from chatbot.graph.builder import build_graph
from chatbot.graph.state import AgentState
from chatbot.memory.threads import PostgresThreadStore, ThreadStore
from chatbot.observability import build_callbacks
from chatbot.tools.registry import build_tools
from chatbot.tools.rerank import build_reranker

logger = logging.getLogger(__name__)


class Resources:
    """Graph, checkpointer and DB pool, created on first request so the app starts even if
    Postgres is down."""

    def __init__(self) -> None:
        self.pool: Pool | None = None
        self.graph: CompiledStateGraph[AgentState] | None = None
        self.checkpointer: AsyncPostgresSaver | None = None
        self.threads: PostgresThreadStore | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        async with self._lock:
            if self.graph is not None:
                return
            settings = get_settings()
            if self.pool is None:
                self.pool = await open_pool(settings)
            checkpointer = AsyncPostgresSaver(self.pool)
            # Creates and migrates the checkpoint tables; a no-op once they're current.
            await checkpointer.setup()
            reranker = build_reranker(settings)
            if reranker:
                # Load the model now rather than during the first search.
                await asyncio.to_thread(reranker.load)
            tools = build_tools(settings, self.pool, build_embeddings(settings), reranker)
            self.checkpointer = checkpointer
            self.threads = PostgresThreadStore(self.pool)
            self.graph = build_graph(settings, tools=tools, checkpointer=checkpointer)
            logger.info(
                "agent ready: model=%s, summary_model=%s, tools=[%s], context limit %d tokens",
                settings.model,
                settings.summary_model,
                ", ".join(t.name for t in tools),
                settings.context_max_tokens,
            )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()


resources = Resources()


async def get_graph() -> CompiledStateGraph[AgentState]:
    await resources.ensure()
    assert resources.graph is not None
    return resources.graph


async def get_checkpointer() -> BaseCheckpointSaver[Any]:
    await resources.ensure()
    assert resources.checkpointer is not None
    return resources.checkpointer


async def get_threads() -> ThreadStore:
    await resources.ensure()
    assert resources.threads is not None
    return resources.threads


async def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    # Stand-in for authentication until Phase 7: callers say who they are, and every thread
    # is scoped to that ID. Real auth replaces this function and nothing else.
    if not x_user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-User-Id header")
    return x_user_id


@lru_cache
def get_callbacks() -> list[BaseCallbackHandler]:
    return build_callbacks(get_settings())


Graph = Annotated[CompiledStateGraph[AgentState], Depends(get_graph)]
Checkpointer = Annotated[BaseCheckpointSaver[Any], Depends(get_checkpointer)]
Threads = Annotated[ThreadStore, Depends(get_threads)]
UserId = Annotated[str, Depends(get_user_id)]
Callbacks = Annotated[list[BaseCallbackHandler], Depends(get_callbacks)]
