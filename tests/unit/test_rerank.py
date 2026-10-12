import pytest

from chatbot.config import Settings
from chatbot.tools.rag import SearchHit
from chatbot.tools.rerank import CrossEncoderReranker, build_reranker


def hit(chunk_id: str, heading: str | None = "Section") -> SearchHit:
    return SearchHit(chunk_id, "doc.md", "Doc", heading, None, f"content {chunk_id}", 0.0)


async def test_orders_hits_by_score_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    reranker = CrossEncoderReranker("unused", cache_dir=None)  # type: ignore[arg-type]
    scores = {"content a": 0.1, "content b": 2.5, "content c": -1.0}
    monkeypatch.setattr(
        reranker, "_score", lambda q, texts: [scores[t.splitlines()[-1]] for t in texts]
    )

    ranked = await reranker.rerank("q", [hit("a"), hit("b"), hit("c")])

    assert [h.chunk_id for h in ranked] == ["b", "a", "c"]
    assert [h.score for h in ranked] == [2.5, 0.1, -1.0]


async def test_empty_input_skips_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    reranker = CrossEncoderReranker("unused", cache_dir=None)  # type: ignore[arg-type]
    monkeypatch.setattr(reranker, "load", lambda: pytest.fail("model should not load"))

    assert await reranker.rerank("q", []) == []


def test_hit_text_includes_title_and_heading() -> None:
    assert CrossEncoderReranker.hit_text(hit("a")) == "Doc\nSection\ncontent a"
    assert CrossEncoderReranker.hit_text(hit("a", heading=None)) == "Doc\ncontent a"


def test_build_reranker_respects_empty_setting() -> None:
    assert build_reranker(Settings(reranker_model="")) is None
    assert build_reranker(Settings(reranker_model=None)) is None
    reranker = build_reranker(Settings(reranker_model="some/model"))
    assert reranker is not None and reranker.model_name == "some/model"
