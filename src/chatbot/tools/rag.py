import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from html import escape
from typing import TYPE_CHECKING, Any

from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool, tool
from pgvector import Vector

from chatbot.config import Settings
from chatbot.db import Conn, Pool
from chatbot.prompts import as_data

if TYPE_CHECKING:
    from chatbot.tools.rerank import Reranker

logger = logging.getLogger(__name__)

RRF_K = 60

VECTOR_SQL = """
    SELECT id::text, doc_id, title, heading, page, content
    FROM chunks
    ORDER BY embedding <=> %s
    LIMIT %s
"""

# plainto_tsquery ANDs every term, which misses most natural-language questions.
# Swapping & for | turns it into "any of these terms", ranked by ts_rank_cd.
FULLTEXT_SQL = """
    WITH q AS (
        SELECT to_tsquery('english', replace(plainto_tsquery('english', %s)::text, '&', '|')) AS q
    )
    SELECT id::text, doc_id, title, heading, page, content
    FROM chunks, q
    WHERE content_tsv @@ q.q
    ORDER BY ts_rank_cd(content_tsv, q.q) DESC
    LIMIT %s
"""


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    doc_id: str
    title: str
    heading: str | None
    page: int | None
    content: str
    score: float

    @property
    def cite_as(self) -> str:
        parts = [self.title]
        if self.heading:
            parts.append(self.heading)
        if self.page:
            parts.append(f"p.{self.page}")
        return " › ".join(parts)


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[str]:
    """Merge ranked ID lists: each ID scores sum(1 / (k + rank)) over the lists it appears in."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: scores[item], reverse=True)


async def hybrid_search(
    conn: Conn, query_vector: list[float], query: str, candidates: int, top_k: int
) -> list[SearchHit]:
    vector_rows = await (
        await conn.execute(VECTOR_SQL, (Vector(query_vector), candidates))
    ).fetchall()
    text_rows = await (await conn.execute(FULLTEXT_SQL, (query, candidates))).fetchall()

    rows = {r["id"]: r for r in [*vector_rows, *text_rows]}
    fused = reciprocal_rank_fusion([[r["id"] for r in vector_rows], [r["id"] for r in text_rows]])
    return [
        SearchHit(
            chunk_id=chunk_id,
            doc_id=rows[chunk_id]["doc_id"],
            title=rows[chunk_id]["title"],
            heading=rows[chunk_id]["heading"],
            page=rows[chunk_id]["page"],
            content=rows[chunk_id]["content"],
            score=1.0 / (RRF_K + rank),
        )
        for rank, chunk_id in enumerate(fused[:top_k], start=1)
    ]


def format_hits(query: str, hits: list[SearchHit]) -> str:
    if not hits:
        return f'No documents in the knowledge base matched "{query}".'
    results = "\n".join(
        f'<result cite_as="{escape(h.cite_as)}">\n{as_data(h.content)}\n</result>' for h in hits
    )
    return f'<kb_results query="{escape(query)}">\n{results}\n</kb_results>'


async def retrieve(
    pool: Pool,
    embeddings: Embeddings,
    settings: Settings,
    query: str,
    reranker: "Reranker | None" = None,
) -> list[SearchHit]:
    """Hybrid search, then (optionally) rerank the best fused candidates and keep the top k."""
    vector = await embeddings.aembed_query(query)
    limit = (
        max(settings.rerank_candidates, settings.retrieval_top_k)
        if reranker
        else settings.retrieval_top_k
    )
    async with pool.connection() as conn:
        hits = await hybrid_search(conn, vector, query, settings.retrieval_candidates, limit)
    if reranker:
        try:
            hits = await reranker.rerank(query, hits)
        except Exception:
            # Fused order is still a good ranking; don't fail the search over the reranker.
            logger.exception("reranking failed; using fused order")
    return hits[: settings.retrieval_top_k]


def build_knowledge_base_tool(
    pool: Pool,
    embeddings: Embeddings,
    settings: Settings,
    reranker: "Reranker | None" = None,
) -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def search_knowledge_base(query: str) -> tuple[str, list[dict[str, Any]]]:
        """Search Acme Outfitters' internal documents: policies (refunds, returns, shipping,
        warranty), product care guides, and the employee handbook.

        Use this for any question about how the company works or what its rules are,
        including rules for customers in specific countries (e.g. EU return rights). Do not
        use it for news, current events, or other companies.

        Args:
            query: A short search query in plain words, e.g. "refund window for sale items".
        """
        start = time.monotonic()
        # Errors propagate to ToolRetryMiddleware, which retries once, then tells the model.
        hits = await retrieve(pool, embeddings, settings, query, reranker)
        logger.info(
            "knowledge base search %r: %d hits in %.1fs (top: %s)",
            query,
            len(hits),
            time.monotonic() - start,
            hits[0].cite_as if hits else "none",
        )
        sources = [{"type": "kb", "cite_as": h.cite_as, **asdict(h)} for h in hits]
        return format_hits(query, hits), sources

    return search_knowledge_base
