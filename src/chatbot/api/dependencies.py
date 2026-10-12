import asyncio
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph.state import CompiledStateGraph

from chatbot.config import get_settings
from chatbot.db import Pool, open_pool
from chatbot.embeddings import build_embeddings
from chatbot.graph.builder import build_graph
from chatbot.graph.state import AgentState
from chatbot.observability import build_callbacks
from chatbot.tools.registry import build_tools
from chatbot.tools.rerank import build_reranker


class Resources:
    """Graph and DB pool, created on first request so the app starts even if Postgres is down."""

    def __init__(self) -> None:
        self.pool: Pool | None = None
        self.graph: CompiledStateGraph[AgentState] | None = None
        self._lock = asyncio.Lock()

    async def get_graph(self) -> CompiledStateGraph[AgentState]:
        async with self._lock:
            if self.graph is None:
                settings = get_settings()
                self.pool = await open_pool(settings)
                reranker = build_reranker(settings)
                if reranker:
                    # Load the model now rather than during the first search.
                    await asyncio.to_thread(reranker.load)
                tools = build_tools(settings, self.pool, build_embeddings(settings), reranker)
                self.graph = build_graph(settings, tools=tools)
            return self.graph

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()


resources = Resources()


async def get_graph() -> CompiledStateGraph[AgentState]:
    return await resources.get_graph()


@lru_cache
def get_callbacks() -> list[BaseCallbackHandler]:
    return build_callbacks(get_settings())


Graph = Annotated[CompiledStateGraph[AgentState], Depends(get_graph)]
Callbacks = Annotated[list[BaseCallbackHandler], Depends(get_callbacks)]
