"""The web search cache on a real Redis (Phase 11, tools/web_search.py): what one pod cached,
another pod reads. Skipped when Redis isn't reachable, unless REQUIRE_REDIS=1 (CI)."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from chatbot.tools.web_search import SearchCache

pytestmark = pytest.mark.integration

URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/0")
RESULTS = [{"title": "REI Returns", "href": "https://rei.example/returns", "body": "30 days"}]


@pytest.fixture
async def query() -> AsyncIterator[str]:
    redis = Redis.from_url(URL, socket_connect_timeout=1)
    try:
        await redis.ping()
    except Exception as e:
        if os.environ.get("REQUIRE_REDIS") == "1":
            raise
        pytest.skip(f"Redis not reachable at {URL}: {e}")
    query = f"test query {uuid.uuid4().hex[:8]}"
    yield query
    await redis.delete(SearchCache(URL, 1, 5).key(query))
    await redis.aclose()


async def test_one_pods_results_are_another_pods_hit(query: str) -> None:
    await SearchCache(URL, 60, 5).put(query, RESULTS)

    assert await SearchCache(URL, 60, 5).get(query.upper()) == RESULTS
    # A different result count is a different search.
    assert await SearchCache(URL, 60, 3).get(query) is None


async def test_results_expire(query: str) -> None:
    cache = SearchCache(URL, 60, 5)
    await cache.put(query, RESULTS)

    assert 0 < await cache.redis.ttl(cache.key(query)) <= 60
