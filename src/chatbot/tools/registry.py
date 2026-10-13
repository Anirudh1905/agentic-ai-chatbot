import logging
from collections.abc import Sequence

from langchain.agents.middleware import ToolRetryMiddleware
from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool

from chatbot.config import Settings
from chatbot.db import Pool
from chatbot.tools import web_search as web_search_module
from chatbot.tools.rag import build_knowledge_base_tool
from chatbot.tools.rerank import Reranker
from chatbot.tools.web_search import build_web_search_tool

logger = logging.getLogger(__name__)

# Read-only tools, so running one twice is harmless. Tools with side effects must not be
# retried automatically.
RETRYABLE = ("search_knowledge_base", "web_search")


def build_tools(
    settings: Settings, pool: Pool, embeddings: Embeddings, reranker: Reranker | None = None
) -> list[BaseTool]:
    tools = [build_knowledge_base_tool(pool, embeddings, settings, reranker)]
    if web_search := build_web_search_tool(settings):
        tools.append(web_search)
    else:
        logger.info("web search is disabled (WEB_SEARCH_ENABLED=false)")
    return tools


def tool_failure_message(error: Exception) -> str:
    logger.warning("tool failed after retrying: %r", error)
    # The exception type only: its message can carry internal details.
    return (
        f"This search failed ({type(error).__name__}). Tell the user it's unavailable right "
        "now, and answer from other sources if you can."
    )


def tool_retry(tools: Sequence[BaseTool]) -> ToolRetryMiddleware | None:
    """Retry a failed search once after a pause, then give the model a message to relay."""
    names: list[BaseTool | str] = [t.name for t in tools if t.name in RETRYABLE]
    if not names:
        return None
    return ToolRetryMiddleware(
        max_retries=1,
        tools=names,
        initial_delay=web_search_module.RETRY_DELAY_SECONDS,
        on_failure=tool_failure_message,
    )
