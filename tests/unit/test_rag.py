from chatbot.tools.rag import SearchHit, format_hits, reciprocal_rank_fusion


def hit(**overrides: object) -> SearchHit:
    fields: dict[str, object] = {
        "chunk_id": "c1",
        "doc_id": "data/refunds.md",
        "title": "Refund Policy",
        "heading": "Sale Items",
        "page": None,
        "content": "Sale items are final.",
        "score": 0.5,
    }
    return SearchHit(**{**fields, **overrides})  # type: ignore[arg-type]


def test_rrf_rewards_items_ranked_by_both_lists() -> None:
    vector = ["a", "b", "c"]
    fulltext = ["c", "d", "a"]

    fused = reciprocal_rank_fusion([vector, fulltext])

    # "a" (ranks 1 and 3) and "c" (ranks 3 and 1) tie and beat items found by only one list.
    assert set(fused[:2]) == {"a", "c"}
    assert fused[2:] == ["b", "d"]


def test_rrf_handles_empty_lists() -> None:
    assert reciprocal_rank_fusion([[], ["x"]]) == ["x"]
    assert reciprocal_rank_fusion([[], []]) == []


def test_cite_as_includes_heading_and_page() -> None:
    assert hit().cite_as == "Refund Policy › Sale Items"
    assert hit(heading=None, page=3).cite_as == "Refund Policy › p.3"


def test_format_hits_wraps_results_and_escapes_attributes() -> None:
    text = format_hits('sale "items"', [hit()])

    assert text.startswith('<kb_results query="sale &quot;items&quot;">')
    assert '<result cite_as="Refund Policy › Sale Items">' in text
    assert "Sale items are final." in text


def test_format_hits_with_no_results() -> None:
    assert "No documents" in format_hits("anything", [])
