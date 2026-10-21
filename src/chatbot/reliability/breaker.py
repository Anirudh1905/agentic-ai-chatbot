"""Circuit breakers on the calls that leave the cluster (Phase 10): each model, the embeddings,
web search.

A breaker counts a dependency's outages (`is_outage`: timeouts, connection errors, 429s and
5xx; not a request the dependency refused, like a 400). BREAKER_FAILURES of them within
BREAKER_WINDOW_SECONDS open it: for BREAKER_COOLDOWN_SECONDS every call fails fast with
`BreakerOpen` (callers fall back, or degrade). Then it's half-open: one call probes, and its
success closes the breaker, its failure opens it again.

The state is shared in Redis, so every API pod and the worker see one breaker per dependency:
one pod's timeouts spare the others theirs. Every key has an expiry, so a pod that dies
mid-probe can't leave a breaker stuck. If Redis can't be reached, each process keeps its own
state in memory until it can again.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Protocol

import httpx
import openai
from redis.asyncio import Redis
from redis.exceptions import RedisError

from chatbot.config import Settings
from chatbot.observability.metrics import BREAKER_OPEN

logger = logging.getLogger(__name__)

# Once open has passed, the breaker stays half-open this much longer, waiting for a probe; with
# no calls at all by then, it closes.
HALF_OPEN_SECONDS = 300
# A probe that hasn't finished by then (its pod died, say) lets another call probe.
PROBE_SECONDS = 120
# After Redis fails, how long a process keeps its breakers in memory before trying it again.
REDIS_RETRY_SECONDS = 30


class BreakerOpen(Exception):
    """A dependency's breaker is open: the call wasn't made."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is unavailable (circuit breaker open)")
        self.name = name


def is_outage(error: BaseException) -> bool:
    """A failure of the dependency itself, which a retry later (or elsewhere) could avoid."""
    if isinstance(error, openai.APIStatusError):
        return error.status_code == 429 or error.status_code >= 500
    return isinstance(error, openai.APIConnectionError | httpx.TransportError | TimeoutError)


class BreakerStore(Protocol):
    async def exists(self, key: str) -> bool: ...

    async def set(self, key: str, seconds: int, only_if_absent: bool = False) -> bool:
        """True if set (always, unless `only_if_absent` and the key exists)."""
        ...

    async def incr(self, key: str, seconds: int) -> int:
        """Add one; a new key expires `seconds` after its first increment."""
        ...

    async def delete(self, *keys: str) -> None: ...


class MemoryBreakerStore:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[int, float]] = {}  # value, monotonic deadline

    def _live(self, key: str) -> tuple[int, float] | None:
        entry = self.keys.get(key)
        if entry and entry[1] <= time.monotonic():
            del self.keys[key]
            return None
        return entry

    async def exists(self, key: str) -> bool:
        return self._live(key) is not None

    async def set(self, key: str, seconds: int, only_if_absent: bool = False) -> bool:
        if only_if_absent and self._live(key):
            return False
        self.keys[key] = (1, time.monotonic() + seconds)
        return True

    async def incr(self, key: str, seconds: int) -> int:
        value, deadline = self._live(key) or (0, time.monotonic() + seconds)
        self.keys[key] = (value + 1, deadline)
        return value + 1

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.keys.pop(key, None)


# INCR and EXPIRE in one step: a process dying between the two would leave a count that
# never expires.
INCR = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


class RedisBreakerStore:
    def __init__(self, url: str) -> None:
        # Short timeouts: a breaker check sits in front of every model call.
        self.redis = Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._incr = self.redis.register_script(INCR)

    async def exists(self, key: str) -> bool:
        return bool(await self.redis.exists(key))

    async def set(self, key: str, seconds: int, only_if_absent: bool = False) -> bool:
        return bool(await self.redis.set(key, 1, ex=seconds, nx=only_if_absent))

    async def incr(self, key: str, seconds: int) -> int:
        return int(await self._incr(keys=[key], args=[seconds]))

    async def delete(self, *keys: str) -> None:
        await self.redis.delete(*keys)


class SharedBreakerStore:
    """Redis, or this process's memory while Redis can't be reached (logged once each way)."""

    def __init__(self, redis: BreakerStore) -> None:
        self.redis = redis
        self.memory = MemoryBreakerStore()
        self._retry_at = 0.0

    async def _run[T](self, op: Callable[[BreakerStore], Awaitable[T]]) -> T:
        if time.monotonic() >= self._retry_at:
            try:
                result = await op(self.redis)
            except (RedisError, OSError):
                if not self._retry_at:
                    logger.warning("circuit breakers can't reach Redis; keeping them in memory")
                self._retry_at = time.monotonic() + REDIS_RETRY_SECONDS
            else:
                if self._retry_at:
                    logger.info("circuit breakers reach Redis again")
                    self._retry_at = 0.0
                return result
        return await op(self.memory)

    async def exists(self, key: str) -> bool:
        return await self._run(lambda s: s.exists(key))

    async def set(self, key: str, seconds: int, only_if_absent: bool = False) -> bool:
        return await self._run(lambda s: s.set(key, seconds, only_if_absent))

    async def incr(self, key: str, seconds: int) -> int:
        return await self._run(lambda s: s.incr(key, seconds))

    async def delete(self, *keys: str) -> None:
        await self._run(lambda s: s.delete(*keys))


class Permit(Enum):
    CLOSED = "closed"
    PROBE = "probe"  # the one call allowed through a half-open breaker


class Breaker:
    def __init__(
        self, name: str, store: BreakerStore, failures: int, window: int, cooldown: int
    ) -> None:
        self.name = name
        self.store = store
        self.failures = failures
        self.window = window
        self.cooldown = cooldown
        prefix = f"breaker/{name}"
        self.count_key, self.open_key = f"{prefix}/failures", f"{prefix}/open"
        self.tripped_key, self.probe_key = f"{prefix}/tripped", f"{prefix}/probe"
        BREAKER_OPEN.labels(name).set(0)

    async def allow(self) -> Permit | None:
        """Whether a call may go ahead (and as what), or None while the breaker is open."""
        if not await self.store.exists(self.tripped_key):
            BREAKER_OPEN.labels(self.name).set(0)
            return Permit.CLOSED  # the usual case: one lookup
        if await self.store.exists(self.open_key):
            BREAKER_OPEN.labels(self.name).set(1)
            return None
        if await self.store.set(self.probe_key, PROBE_SECONDS, only_if_absent=True):
            return Permit.PROBE
        return None  # another call is probing

    async def succeeded(self, permit: Permit) -> None:
        if permit is Permit.PROBE:
            await self.store.delete(self.tripped_key, self.probe_key, self.count_key)
            BREAKER_OPEN.labels(self.name).set(0)
            logger.info("%s is back: closed its circuit breaker", self.name)

    async def failed(self, permit: Permit) -> None:
        if permit is Permit.PROBE:
            await self._open()
            await self.store.delete(self.probe_key)
            logger.warning("%s still failing: circuit breaker open again", self.name)
        elif await self.store.incr(self.count_key, self.window) >= self.failures:
            await self._open()
            await self.store.delete(self.count_key)
            logger.warning(
                "%s failed %d times in %d s: circuit breaker open for %d s",
                self.name,
                self.failures,
                self.window,
                self.cooldown,
            )

    async def _open(self) -> None:
        await self.store.set(self.open_key, self.cooldown)
        await self.store.set(self.tripped_key, self.cooldown + HALF_OPEN_SECONDS)
        BREAKER_OPEN.labels(self.name).set(1)

    async def call[T](
        self, fn: Callable[[], Awaitable[T]], counts: Callable[[BaseException], bool] = is_outage
    ) -> T:
        """`fn()` through the breaker: raises BreakerOpen without calling it while open. Errors
        `counts` doesn't take for an outage mean the dependency answered."""
        permit = await self.allow()
        if permit is None:
            raise BreakerOpen(self.name)
        try:
            result = await fn()
        except Exception as e:
            await (self.failed(permit) if counts(e) else self.succeeded(permit))
            raise
        await self.succeeded(permit)
        return result


class Breakers:
    """One breaker per dependency name, all on one store."""

    def __init__(self, settings: Settings, store: BreakerStore | None = None) -> None:
        self.settings = settings
        self.store = store or MemoryBreakerStore()
        self._breakers: dict[str, Breaker] = {}

    @classmethod
    def shared(cls, settings: Settings) -> "Breakers":
        """In Redis (REDIS_URL), shared with every other process; in memory without it."""
        if not settings.redis_url:
            return cls(settings)
        return cls(settings, SharedBreakerStore(RedisBreakerStore(settings.redis_url)))

    def get(self, name: str) -> Breaker:
        if name not in self._breakers:
            s = self.settings
            self._breakers[name] = Breaker(
                name,
                self.store,
                s.breaker_failures,
                s.breaker_window_seconds,
                s.breaker_cooldown_seconds,
            )
        return self._breakers[name]
