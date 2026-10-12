import logging

from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool

from chatbot.config import Settings
from chatbot.db import Pool
from chatbot.tools.rag import build_knowledge_base_tool
from chatbot.tools.rerank import Reranker
from chatbot.tools.web_search import build_web_search_tool

logger = logging.getLogger(__name__)


def build_tools(
    settings: Settings, pool: Pool, embeddings: Embeddings, reranker: Reranker | None = None
) -> list[BaseTool]:
    tools = [build_knowledge_base_tool(pool, embeddings, settings, reranker)]
    if web_search := build_web_search_tool(settings):
        tools.append(web_search)
    else:
        logger.info("web search is disabled (WEB_SEARCH_ENABLED=false)")
    return tools
