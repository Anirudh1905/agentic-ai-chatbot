"""Retrieval: does the knowledge base search find the section that answers the question, in its
top k? Scored in code against the labelled document and heading."""

from chatbot.config import Settings
from chatbot.storage.db import open_pool
from chatbot.storage.embeddings import build_embeddings
from chatbot.tools.rag import SearchHit, retrieve
from chatbot.tools.rerank import build_reranker
from evals.golden import Case
from evals.scoring import Outcome


def matches(hit: SearchHit, expect: dict[str, str | None]) -> bool:
    doc = expect["doc"] or ""
    return hit.doc_id.endswith(doc) and expect["heading"] in (None, hit.heading)


async def run(settings: Settings, cases: list[Case]) -> dict[str, Outcome]:
    pool = await open_pool(settings)
    embeddings = build_embeddings(settings)
    reranker = build_reranker(settings)
    outcomes = {}
    try:
        for case in cases:
            if not case.expect:
                continue
            hits = await retrieve(pool, embeddings, settings, case.question, reranker)
            rank = next((i for i, h in enumerate(hits, 1) if matches(h, case.expect)), None)
            outcomes[case.id] = Outcome(
                rank is not None,
                1 / rank if rank else 0.0,  # reciprocal rank, for MRR
                f"rank {rank}" if rank else f"missed; got {[h.cite_as for h in hits[:3]]}",
            )
    finally:
        await pool.close()
    return outcomes
