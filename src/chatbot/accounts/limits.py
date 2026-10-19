"""Rate limits per user and per IP, and a daily token budget per user (Phase 8).

Counted in Redis through the `limits` library, so every API process shares the same counts:
- **Rate limits:** a moving window (RATE_LIMIT_PER_USER, RATE_LIMIT_PER_IP, e.g. "120/minute"),
  and a tighter one per IP for registering and logging in (RATE_LIMIT_LOGIN). A request over a
  limit gets 429 with `Retry-After`.
- **Token budget:** the model tokens (input + output) a user's agent runs used today (UTC),
  added after every model call by `SpendTokens`. A request that would start a run once the
  budget is spent gets 429 until midnight UTC. A run that starts under the budget finishes:
  it's checked before a run, not in the middle of one. Summaries and memory pushes aren't
  counted; they're our cost, not the user's request.

If Redis can't be reached, requests go through and a warning is logged: the limits protect
cost, and an outage of the counter store shouldn't take the chatbot down with it.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from limits import RateLimitItem, parse
from limits.aio.storage import Storage
from limits.aio.strategies import MovingWindowRateLimiter
from limits.storage import storage_from_string

from chatbot.config import RunContext, Settings
from chatbot.observability.metrics import LIMITED

logger = logging.getLogger(__name__)


def seconds_to_midnight(now: datetime) -> int:
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    return max(1, int((midnight - now).total_seconds()))


class UsageLimits:
    def __init__(self, settings: Settings, storage_uri: str | None = None) -> None:
        uri = storage_uri or settings.require("redis_url")
        storage = storage_from_string(uri if uri.startswith("async+") else f"async+{uri}")
        assert isinstance(storage, Storage)
        self.storage = storage
        self.window = MovingWindowRateLimiter(storage)
        self.per_user = parse(settings.rate_limit_per_user)
        self.per_ip = parse(settings.rate_limit_per_ip)
        self.per_login = parse(settings.rate_limit_login)
        self.budget = settings.daily_token_budget

    async def limit_user(self, user_id: str) -> None:
        await self._hit(self.per_user, "user", user_id)

    async def limit_ip(self, ip: str) -> None:
        await self._hit(self.per_ip, "ip", ip)

    async def limit_login(self, ip: str) -> None:
        await self._hit(self.per_login, "login", ip)

    async def _hit(self, item: RateLimitItem, kind: str, key: str) -> None:
        try:
            if await self.window.hit(item, kind, key):
                return
            reset = (await self.window.get_window_stats(item, kind, key)).reset_time
        except Exception:
            logger.warning("rate limits unavailable (is Redis up?); letting the request through")
            return
        retry = max(1, int(reset - time.time()) + 1)
        LIMITED.labels(kind).inc()
        logger.info("rate limited (%s, %s)", kind, item)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many requests ({item} per {kind}). Try again in {retry} s.",
            headers={"Retry-After": str(retry)},
        )

    @staticmethod
    def _budget_key(user_id: str, now: datetime) -> str:
        return f"tokens/{user_id}/{now:%Y-%m-%d}"

    async def used_today(self, user_id: str) -> int:
        return int(await self.storage.get(self._budget_key(user_id, datetime.now(UTC))) or 0)

    async def check_budget(self, user_id: str) -> None:
        if self.budget <= 0:
            return
        try:
            used = await self.used_today(user_id)
        except Exception:
            logger.warning("token budget unavailable (is Redis up?); letting the request through")
            return
        if used >= self.budget:
            retry = seconds_to_midnight(datetime.now(UTC))
            LIMITED.labels("budget").inc()
            logger.info("daily token budget spent (%d of %d)", used, self.budget)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Today's token budget ({self.budget:,}) is used up. It resets at midnight UTC.",
                headers={"Retry-After": str(retry)},
            )

    async def spend(self, user_id: str, tokens: int) -> None:
        now = datetime.now(UTC)
        try:
            # Expires a little after midnight, once the day's count is no use.
            await self.storage.incr(
                self._budget_key(user_id, now), seconds_to_midnight(now) + 60, amount=tokens
            )
        except Exception:
            logger.warning("couldn't count %d tokens for the budget (is Redis up?)", tokens)


class SpendTokens(AgentMiddleware[AgentState[Any], RunContext]):
    """After every model call of an agent run, add its tokens to the user's daily budget."""

    def __init__(self, limits: UsageLimits) -> None:
        super().__init__()
        self.limits = limits

    async def aafter_model(
        self, state: AgentState[Any], runtime: Runtime[RunContext]
    ) -> dict[str, Any] | None:
        reply = state["messages"][-1]
        usage = reply.usage_metadata if isinstance(reply, AIMessage) else None
        if usage and runtime.context is not None:
            await self.limits.spend(runtime.context.user_id, usage["total_tokens"])
        return None
