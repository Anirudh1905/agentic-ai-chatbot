"""Circuit breakers shared in Redis (Phase 10, reliability/breaker.py): what one process sees,
every process sees. Skipped when Redis isn't reachable, unless REQUIRE_REDIS=1 (CI)."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from fakes import outage
from redis.asyncio import Redis

from chatbot.config import Settings
from chatbot.reliability.breaker import (
    BreakerOpen,
    Breakers,
    Permit,
    RedisBreakerStore,
    SharedBreakerStore,
)

pytestmark = pytest.mark.integration

URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/0")
SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    breaker_failures=3,
    breaker_window_seconds=60,
    breaker_cooldown_seconds=1,
)


@pytest.fixture
async def name() -> AsyncIterator[str]:
    redis = Redis.from_url(URL, socket_connect_timeout=1)
    try:
        await redis.ping()
    except Exception as e:
        if os.environ.get("REQUIRE_REDIS") == "1":
            raise
        pytest.skip(f"Redis not reachable at {URL}: {e}")
    name = f"test-{uuid.uuid4().hex[:8]}"
    yield name
    if keys := [k async for k in redis.scan_iter(f"breaker/{name}/*")]:
        await redis.delete(*keys)
    await redis.aclose()


def pod() -> Breakers:
    """One process's breakers, on the shared Redis."""
    return Breakers(SETTINGS, SharedBreakerStore(RedisBreakerStore(URL)))


async def fail() -> None:
    raise outage()


async def test_one_pods_failures_open_the_breaker_for_every_pod(name: str) -> None:
    api, worker = pod(), pod()
    for _ in range(3):
        with pytest.raises(Exception, match="timed out"):
            await api.get(name).call(fail)

    with pytest.raises(BreakerOpen):
        await worker.get(name).call(fail)


async def test_only_one_pod_probes_and_its_success_closes_it_for_all(name: str) -> None:
    a, b = pod(), pod()
    for _ in range(3):
        with pytest.raises(Exception, match="timed out"):
            await a.get(name).call(fail)
    redis = Redis.from_url(URL)
    await redis.delete(f"breaker/{name}/open")  # the cooldown is over

    probe = await a.get(name).allow()
    assert probe is Permit.PROBE
    assert await b.get(name).allow() is None
    await a.get(name).succeeded(probe)

    assert await b.get(name).allow() is Permit.CLOSED
    await redis.aclose()


async def test_every_key_expires(name: str) -> None:
    breakers = pod()
    with pytest.raises(Exception, match="timed out"):
        await breakers.get(name).call(fail)
    for _ in range(2):
        with pytest.raises(Exception, match="timed out"):
            await breakers.get(name).call(fail)
    redis = Redis.from_url(URL)

    keys = [k async for k in redis.scan_iter(f"breaker/{name}/*")]
    ttls = [await redis.ttl(k) for k in keys]
    await redis.aclose()

    assert keys and all(t > 0 for t in ttls)  # none could stay forever
