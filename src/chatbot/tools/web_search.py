import asyncio
import hashlib
import json
import logging
import re
import time
from html import escape
from typing import Any

from ddgs import DDGS
from langchain_core.tools import BaseTool, tool
from redis.asyncio import Redis
from redis.exceptions import RedisError

from chatbot.config import Settings
from chatbot.observability.metrics import WEB_SEARCH_CACHE
from chatbot.prompts import as_data
from chatbot.reliability.breaker import BreakerOpen, Breakers

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 800
# DuckDuckGo alone is fastest but sometimes returns nothing; "auto" queries several engines.
BACKENDS = ("duckduckgo", "auto")
# Keyless search is rate-limited (ddgs raises "No results found."). When every backend fails,
# ToolRetryMiddleware waits this long and runs the tool once more (see tools/registry.py).
RETRY_DELAY_SECONDS = 1.5
# While web search's circuit breaker is open (reliability/breaker.py), the tool answers this at
# once instead of trying.
UNAVAILABLE = (
    "Web search is unavailable right now. Tell the user, and answer from the knowledge base or "
    "what you know if you can."
)


def is_ad(result: dict[str, Any]) -> bool:
    return "/aclick?" in result.get("href", "")


def format_results(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f'The web search for "{query}" returned no results.'
    items = []
    for r in results:
        cite = f"[{r['title']}]({r['href']})"
        snippet = as_data((r.get("body") or "")[:SNIPPET_CHARS])
        items.append(f'<result cite_as="{escape(cite)}">\n{snippet}\n</result>')
    return f'<web_results query="{escape(query)}">\n' + "\n".join(items) + "\n</web_results>"


async def search(query: str, settings: Settings) -> list[dict[str, Any]]:
    """Try each backend in turn; raise only if every one fails."""
    last_error: Exception | None = None
    for backend in BACKENDS:
        try:
            # ddgs is synchronous; run it in a thread so the event loop keeps serving.
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    DDGS(timeout=int(settings.web_search_timeout)).text,
                    query,
                    max_results=settings.web_search_max_results,
                    backend=backend,
                ),
                timeout=settings.web_search_timeout,
            )
        except Exception as e:
            logger.warning("web search (%s) failed: %r", backend, e)
            last_error = e
            continue
        results = [r for r in results if not is_ad(r)]
        if results:
            logger.debug("web search (%s) returned %d results", backend, len(results))
            return results
    if last_error is not None:
        raise last_error
    return []


class SearchCache:
    """Web search results in Redis for WEB_SEARCH_CACHE_SECONDS (Phase 11), by query (case and
    spacing aside) and result count. A cache, not a dependency: while Redis is down, searches
    just aren't cached."""

    def __init__(self, url: str, seconds: int, max_results: int) -> None:
        # Timeouts well under a search's: a slow Redis mustn't slow searches down.
        self.redis = Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self.seconds = seconds
        self.max_results = max_results
        self.failing = False

    def key(self, query: str) -> str:
        normal = re.sub(r"\s+", " ", query).strip().lower()
        return f"web_search/{self.max_results}/{hashlib.sha256(normal.encode()).hexdigest()}"

    async def get(self, query: str) -> list[dict[str, Any]] | None:
        try:
            cached = await self.redis.get(self.key(query))
        except RedisError as e:
            self._failed(e)
            return None
        self.failing = False
        WEB_SEARCH_CACHE.labels("hit" if cached else "miss").inc()
        return json.loads(cached) if cached else None

    async def put(self, query: str, results: list[dict[str, Any]]) -> None:
        try:
            await self.redis.set(self.key(query), json.dumps(results), ex=self.seconds)
        except RedisError as e:
            self._failed(e)

    def _failed(self, error: RedisError) -> None:
        # Once per outage, not once per search.
        if not self.failing:
            logger.warning("web search cache unavailable (%r); searching uncached", error)
        self.failing = True


def build_search_cache(settings: Settings) -> SearchCache | None:
    if not (settings.redis_url and settings.web_search_cache_seconds):
        return None
    return SearchCache(
        settings.redis_url, settings.web_search_cache_seconds, settings.web_search_max_results
    )


def build_web_search_tool(
    settings: Settings, breakers: Breakers | None = None, cache: SearchCache | None = None
) -> BaseTool | None:
    if not settings.web_search_enabled:
        return None
    breaker = (breakers or Breakers(settings)).get("web_search")

    @tool(response_format="content_and_artifact")
    async def web_search(query: str) -> tuple[str, list[dict[str, Any]]]:
        """Search the public web. Use for current events, news, weather, prices, other
        companies, public facts, or anything that changes over time.

        Do not use it for Acme Outfitters' own policies or internal information; use
        search_knowledge_base for those.

        Args:
            query: A search engine query, e.g. "REI return policy 2026".
        """
        start = time.monotonic()
        results = await cache.get(query) if cache else None
        hit = results is not None
        if results is None:
            # Errors propagate to ToolRetryMiddleware, which retries once, then tells the
            # model. Every failure counts toward the breaker: by then each backend has failed.
            try:
                results = await breaker.call(lambda: search(query, settings), counts=lambda e: True)
            except BreakerOpen:
                logger.info("web search skipped: its circuit breaker is open")
                return UNAVAILABLE, []
            if cache and results:  # no results may be a rate limit: try again next time
                await cache.put(query, results)
        logger.info(
            "web search %r: %d results in %.1fs%s",
            query,
            len(results),
            time.monotonic() - start,
            " (cached)" if hit else "",
        )
        sources = [
            {
                "type": "web",
                "title": r["title"],
                "url": r["href"],
                "cite_as": f"[{r['title']}]({r['href']})",
            }
            for r in results
        ]
        return format_results(query, results), sources

    return web_search
