"""Long-term memory in a real AsyncPostgresStore with a vector index (see conftest.py)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FactMemoryModel
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.messages import HumanMessage
from langgraph.store.postgres.aio import AsyncPostgresStore

from chatbot.config import Settings
from chatbot.db import Pool, open_pool
from chatbot.memory.long_term import MemoryWriter, list_memories, namespace

pytestmark = pytest.mark.integration

DIMS = Settings().embedding_dimensions


@pytest.fixture
def user() -> str:
    return f"user-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    pool = await open_pool(Settings(database_url=database_url))
    yield pool
    await pool.close()


async def indexed_store(pool: Pool) -> AsyncPostgresStore:
    index = {"dims": DIMS, "embed": DeterministicFakeEmbedding(size=DIMS), "fields": ["content"]}
    ttl = {"refresh_on_read": False, "omit_expired": True}
    store = AsyncPostgresStore(pool, index=index, ttl=ttl)  # type: ignore[arg-type]
    await store.setup()
    return store


def turns(*texts: str) -> list[HumanMessage]:
    return [HumanMessage(text, id=uuid.uuid4().hex) for text in texts]


async def contents(store: AsyncPostgresStore, user: str) -> list[str]:
    return sorted(i.value["content"] for i in await list_memories(store, user))


async def expiry(pool: Pool, prefix: tuple[str, ...]) -> dict[str, datetime | None]:
    async with pool.connection() as conn:
        rows = await conn.execute(
            "SELECT coalesce(value->>'content', value->>'text') AS text, expires_at "
            "FROM store WHERE prefix = %s",
            (".".join(prefix),),
        )
        return {r["text"]: r["expires_at"] for r in await rows.fetchall()}


async def test_memories_update_and_survive_a_restart(
    database_url: str, pool: Pool, user: str
) -> None:
    store = await indexed_store(pool)
    writer = MemoryWriter(FactMemoryModel())

    await writer.push(store, user, "t1", turns("fact: colour=pink", "fact: city=Pune"))
    await writer.push(store, user, "t2", turns("fact: colour=red"))

    fresh = await open_pool(Settings(database_url=database_url))
    try:
        restarted = await indexed_store(fresh)
        assert await contents(restarted, user) == ["fact: city=Pune", "fact: colour=red"]
    finally:
        await fresh.close()


async def test_two_processes_pushing_the_same_thread_store_each_fact_once(
    pool: Pool, user: str
) -> None:
    store = await indexed_store(pool)
    messages = turns("fact: contact=WhatsApp")
    # Separate writers stand in for two API processes (a token breach and the TTL check at
    # once): their in-process locks don't see each other, so only the Postgres advisory lock
    # keeps them from both reading the thread's cursor before either moves it.
    first, second = MemoryWriter(FactMemoryModel()), MemoryWriter(FactMemoryModel())

    await asyncio.gather(
        first.push(store, user, "t1", messages), second.push(store, user, "t1", messages)
    )

    assert await contents(store, user) == ["fact: contact=WhatsApp"]


async def test_a_temporary_memory_expires(pool: Pool, user: str) -> None:
    store = await indexed_store(pool)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()

    await MemoryWriter(FactMemoryModel()).push(
        store, user, "t1", turns(f"fact: trip=Coorg until={tomorrow}", "fact: colour=red")
    )

    found = await expiry(pool, namespace(user))
    assert found["fact: colour=red"] is None
    # At the end of valid_until (UTC), give or take the store's whole-minute TTL.
    end = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time(), UTC)
    assert abs(found["fact: trip=Coorg"] - end) < timedelta(minutes=2)  # type: ignore[operator]

    # Once expired, reads skip it (the sweeper deletes it later).
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE store SET expires_at = NOW() - interval '1 minute' WHERE prefix = %s "
            "AND value->>'content' = 'fact: trip=Coorg'",
            (".".join(namespace(user)),),
        )
    assert await contents(store, user) == ["fact: colour=red"]


async def test_many_users_pushing_at_once_dont_exhaust_the_pool(pool: Pool, user: str) -> None:
    store = await indexed_store(pool)
    writer = MemoryWriter(FactMemoryModel(), concurrency=Settings().memory_write_concurrency)
    users = [f"{user}-{i}" for i in range(12)]  # more than the pool's 10 connections
    await asyncio.gather(
        *(writer.push(store, u, "t1", turns(f"fact: city=City{i}")) for i, u in enumerate(users))
    )

    assert [await contents(store, u) for u in users] == [[f"fact: city=City{i}"] for i in range(12)]
