"""Long-term memories in a real AsyncPostgresStore with a vector index (see conftest.py)."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fakes import FactMemoryModel
from langchain_core.embeddings import DeterministicFakeEmbedding
from langgraph.store.postgres.aio import AsyncPostgresStore

from chatbot.config import Settings
from chatbot.db import Pool, open_pool
from chatbot.memory.long_term import MemoryWriter, list_memories, wait_for_memory_writes

pytestmark = pytest.mark.integration

DIMS = Settings().embedding_dimensions


@pytest.fixture
def user(monkeypatch: pytest.MonkeyPatch) -> str:
    user = f"user-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr("chatbot.memory.long_term.current_user", lambda: user)
    return user


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    pool = await open_pool(Settings(database_url=database_url))
    yield pool
    await pool.close()


async def indexed_store(pool: Pool) -> AsyncPostgresStore:
    index = {"dims": DIMS, "embed": DeterministicFakeEmbedding(size=DIMS), "fields": ["content"]}
    store = AsyncPostgresStore(pool, index=index)  # type: ignore[arg-type]
    await store.setup()
    return store


async def contents(store: AsyncPostgresStore, user: str) -> list[str]:
    return sorted(i.value["content"] for i in await list_memories(store, user))


async def test_memories_update_and_survive_a_restart(
    database_url: str, pool: Pool, user: str
) -> None:
    store = await indexed_store(pool)
    writer = MemoryWriter(FactMemoryModel())

    writer.schedule(store, "- fact: colour=pink\n- fact: city=Pune")
    await wait_for_memory_writes()
    writer.schedule(store, "- fact: colour=red")
    await wait_for_memory_writes()

    fresh = await open_pool(Settings(database_url=database_url))
    try:
        assert await contents(await indexed_store(fresh), user) == [
            "fact: city=Pune",
            "fact: colour=red",
        ]
    finally:
        await fresh.close()


async def test_two_processes_writing_the_same_fact_store_it_once(pool: Pool, user: str) -> None:
    store = await indexed_store(pool)
    # Separate writers stand in for two API processes: their in-process locks don't see each
    # other, so only the Postgres advisory lock keeps them from both adding the fact.
    first, second = MemoryWriter(FactMemoryModel()), MemoryWriter(FactMemoryModel())

    await asyncio.gather(
        first._write(store, user, "- fact: contact=WhatsApp"),
        second._write(store, user, "- fact: contact=WhatsApp"),
    )

    assert await contents(store, user) == ["fact: contact=WhatsApp"]
