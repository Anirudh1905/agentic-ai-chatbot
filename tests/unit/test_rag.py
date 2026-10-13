import pytest
from fakes import ScriptedChatModel, tool_call
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage

from chatbot.agent.builder import build_graph
from chatbot.config import Settings
from chatbot.tools import web_search
from chatbot.tools.rag import (
    SearchHit,
    build_knowledge_base_tool,
    format_hits,
    reciprocal_rank_fusion,
)


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


async def test_failed_search_is_retried_once_then_reported_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_search, "RETRY_DELAY_SECONDS", 0)
    attempts: list[str] = []

    class DownEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise NotImplementedError

        def embed_query(self, text: str) -> list[float]:
            attempts.append(text)
            raise ConnectionError("embeddings endpoint down")

    tool = build_knowledge_base_tool(None, DownEmbeddings(), Settings())  # type: ignore[arg-type]
    llm = ScriptedChatModel(
        responses=[tool_call("search_knowledge_base", query="refunds"), AIMessage("It's down.")]
    )
    graph = build_graph(Settings(), tools=[tool], llm=llm)

    result = await graph.ainvoke({"messages": [HumanMessage("refunds?")]})

    assert attempts == ["refunds", "refunds"]
    tool_message = result["messages"][2]
    assert tool_message.status == "error"
    assert "This search failed (ConnectionError)" in tool_message.text
