import asyncio
import logging
import time
from html import escape
from typing import Any

from ddgs import DDGS
from langchain_core.tools import BaseTool, tool

from chatbot.config import Settings
from chatbot.prompts import as_data

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 800
# DuckDuckGo alone is fastest but sometimes returns nothing; "auto" queries several engines.
BACKENDS = ("duckduckgo", "auto")
# Keyless search is rate-limited (ddgs raises "No results found."). When every backend fails,
# ToolRetryMiddleware waits this long and runs the tool once more (see tools/registry.py).
RETRY_DELAY_SECONDS = 1.5


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


def build_web_search_tool(settings: Settings) -> BaseTool | None:
    if not settings.web_search_enabled:
        return None

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
        # Errors propagate to ToolRetryMiddleware, which retries once, then tells the model.
        results = await search(query, settings)
        logger.info(
            "web search %r: %d results in %.1fs", query, len(results), time.monotonic() - start
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
