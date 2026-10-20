"""Ingestion and hybrid search against a real Postgres + pgvector (see conftest.py)."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from chatbot.config import Settings
from chatbot.ingestion.pipeline import ingest_path
from chatbot.reliability.breaker import BreakerOpen
from chatbot.storage.db import Pool, open_pool
from chatbot.tools.rag import (
    KEYWORD_ONLY,
    SearchHit,
    build_knowledge_base_tool,
    hybrid_search,
    retrieve,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    pool = await open_pool(Settings(database_url=database_url))
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE chunks")
    yield pool
    await pool.close()


@pytest.fixture
def embeddings() -> DeterministicFakeEmbedding:
    return DeterministicFakeEmbedding(size=1536)


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "kb"
    root.mkdir()
    (root / "refunds.md").write_text(
        "# Refund Policy\n\n## Sale Items\n\nClearance purchases are final sale.\n\n"
        "## Timing\n\nRefunds are issued within five business days.\n"
    )
    (root / "shipping.md").write_text(
        "# Shipping\n\n## Express\n\nExpress delivery takes one to two days.\n"
    )
    return root


SETTINGS = Settings(chunk_size_tokens=200, chunk_overlap_tokens=0)


async def test_ingest_is_idempotent_and_tracks_changes(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    first = await ingest_path(pool, embeddings, SETTINGS, docs)
    assert {r.doc_id: r.added for r in first} == {"kb/refunds.md": 2, "kb/shipping.md": 1}

    second = await ingest_path(pool, embeddings, SETTINGS, docs)
    assert sum(r.added for r in second) == 0
    assert sum(r.kept for r in second) == 3

    (docs / "shipping.md").write_text(
        "# Shipping\n\n## Express\n\nExpress delivery takes one business day.\n"
    )
    third = await ingest_path(pool, embeddings, SETTINGS, docs)
    shipping = next(r for r in third if r.doc_id == "kb/shipping.md")
    assert (shipping.added, shipping.kept, shipping.removed) == (1, 0, 1)


async def test_prune_removes_deleted_files(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    await ingest_path(pool, embeddings, SETTINGS, docs)
    (docs / "shipping.md").unlink()

    await ingest_path(pool, embeddings, SETTINGS, docs, prune=True)

    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT DISTINCT doc_id FROM chunks")).fetchall()
    assert [r["doc_id"] for r in rows] == ["kb/refunds.md"]


async def test_hybrid_search_finds_keyword_match(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    await ingest_path(pool, embeddings, SETTINGS, docs)
    query = "how long do refunds take"

    async with pool.connection() as conn:
        hits = await hybrid_search(conn, embeddings.embed_query(query), query, 10, 3)

    # Fake embeddings rank randomly, so the full-text side decides the winner.
    assert hits[0].cite_as == "Refund Policy › Timing"
    assert "five business days" in hits[0].content


async def test_knowledge_base_tool_returns_citable_results(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    await ingest_path(pool, embeddings, SETTINGS, docs)
    tool = build_knowledge_base_tool(pool, embeddings, SETTINGS)

    message = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": "1",
            "name": "search_knowledge_base",
            "args": {"query": "express delivery"},
        }
    )

    assert '<result cite_as="Shipping › Express">' in message.content
    assert message.artifact[0]["doc_id"] == "kb/shipping.md"


class ReverseReranker:
    def __init__(self) -> None:
        self.seen: list[int] = []

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        self.seen.append(len(hits))
        return list(reversed(hits))


class BrokenReranker:
    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        raise RuntimeError("model crashed")


async def test_retrieve_reranks_candidates_then_keeps_top_k(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    await ingest_path(pool, embeddings, SETTINGS, docs)
    settings = SETTINGS.model_copy(update={"retrieval_top_k": 2, "rerank_candidates": 3})
    reranker = ReverseReranker()
    query = "refunds express delivery"

    fused = await retrieve(pool, embeddings, settings, query)
    reranked = await retrieve(pool, embeddings, settings, query, reranker)

    assert reranker.seen == [3]
    assert len(reranked) == 2
    assert reranked[0].chunk_id != fused[0].chunk_id


async def test_retrieve_falls_back_to_fused_order_when_reranker_fails(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    await ingest_path(pool, embeddings, SETTINGS, docs)
    query = "how long do refunds take"

    hits = await retrieve(pool, embeddings, SETTINGS, query, BrokenReranker())

    assert hits[0].cite_as == "Refund Policy › Timing"


async def test_search_goes_on_with_keywords_when_the_embeddings_are_down(
    pool: Pool, embeddings: DeterministicFakeEmbedding, docs: Path
) -> None:
    await ingest_path(pool, embeddings, SETTINGS, docs)

    class Down(DeterministicFakeEmbedding):
        async def aembed_query(self, text: str) -> list[float]:
            raise BreakerOpen("embeddings")

    tool = build_knowledge_base_tool(pool, Down(size=embeddings.size), SETTINGS)
    text = await tool.ainvoke({"query": "how long do refunds take"})

    assert text.startswith(KEYWORD_ONLY)
    assert "Refund Policy › Timing" in text
