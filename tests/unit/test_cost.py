"""Cost and latency (Phase 11): what the model is sent, and the web search cache."""

from typing import Any

import pytest
from fakes import ScriptedChatModel, make_lookup_tool, tool_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError

from chatbot.agent.builder import CLEARED_TOOL_RESULT, build_graph
from chatbot.agent.middleware import system_prompt
from chatbot.config import Settings
from chatbot.tools import web_search as web_search_module
from chatbot.tools.rag import CHARS_PER_TOKEN, SearchHit, within_tokens
from chatbot.tools.web_search import SearchCache, build_web_search_tool

# --- Prompt caching: the prefix stays the same from call to call ---


async def test_every_call_of_a_thread_starts_with_the_same_system_prompt() -> None:
    llm = ScriptedChatModel(
        responses=[tool_call("lookup", query="a"), AIMessage("one"), AIMessage("two")]
    )
    graph = build_graph(
        Settings(), tools=[make_lookup_tool()], llm=llm, checkpointer=InMemorySaver()
    )
    config: Any = {"configurable": {"thread_id": "t"}}

    await graph.ainvoke({"messages": [HumanMessage("first")]}, config)
    await graph.ainvoke({"messages": [HumanMessage("second")]}, config)

    assert len(llm.calls) == 3
    assert len({call[0].text for call in llm.calls}) == 1


def test_recalled_memories_come_after_the_static_prompt() -> None:
    # dynamic_prompt sets the request's system prompt; read it back through a stub handler.
    class Captured:
        def __init__(self, state: dict[str, Any]) -> None:
            self.state = state
            self.system_message: Any = None

        def override(self, **kwargs: Any) -> "Captured":
            self.system_message = kwargs.get("system_message")
            return self

    def built(state: dict[str, Any]) -> str:
        request = Captured(state)
        system_prompt.wrap_model_call(request, lambda r: r)  # type: ignore[arg-type, return-value]
        return str(request.system_message.text)

    plain = built({})
    with_memories = built({"recalled_memories": ["2026-10-01: prefers trail shoes"]})

    assert with_memories.startswith(plain)
    assert "prefers trail shoes" in with_memories.removeprefix(plain)


async def test_cached_input_tokens_are_counted() -> None:
    def cached() -> float:
        labels = {"model": "unknown"}
        return REGISTRY.get_sample_value("chatbot_model_cached_tokens_total", labels) or 0.0

    reply = AIMessage(
        "ok",
        usage_metadata={
            "input_tokens": 2000,
            "output_tokens": 5,
            "total_tokens": 2005,
            "input_token_details": {"cache_read": 1536},
        },
    )
    before = cached()

    await build_graph(Settings(), llm=ScriptedChatModel(responses=[reply])).ainvoke(
        {"messages": [HumanMessage("hi")]}
    )

    assert cached() - before == 1536


# --- The context cap ---


def chunk(chunk_id: str, tokens: int) -> SearchHit:
    return SearchHit(chunk_id, "d", "Doc", None, None, "x" * tokens * CHARS_PER_TOKEN, 0.1)


def test_retrieval_keeps_the_best_ranked_chunks_that_fit() -> None:
    hits = [chunk("a", 1000), chunk("b", 1000), chunk("c", 1000), chunk("d", 10)]

    assert [h.chunk_id for h in within_tokens(hits, 2500)] == ["a", "b"]


def test_retrieval_always_keeps_the_best_chunk() -> None:
    assert [h.chunk_id for h in within_tokens([chunk("a", 5000)], 2500)] == ["a"]


def history(results: int) -> list[Any]:
    """`results` earlier turns, each a search and an answer, then a new question."""
    messages: list[Any] = []
    for i in range(results):
        messages += [
            HumanMessage(f"question {i}"),
            tool_call("lookup", call_id=f"c{i}", query=str(i)),
            ToolMessage("result " * 400, tool_call_id=f"c{i}", name="lookup"),
            AIMessage(f"answer {i}"),
        ]
    return [*messages, HumanMessage("next question")]


async def test_old_tool_results_are_cleared_from_a_long_context() -> None:
    llm = ScriptedChatModel(responses=[AIMessage("ok")])
    settings = Settings(context_clear_tool_results_tokens=1000, context_keep_tool_results=1)
    graph = build_graph(settings, tools=[make_lookup_tool()], llm=llm)

    result = await graph.ainvoke({"messages": history(3)})

    sent = [m.text for m in llm.calls[0] if isinstance(m, ToolMessage)]
    assert sent == [CLEARED_TOOL_RESULT, CLEARED_TOOL_RESULT, "result " * 400]
    # The thread keeps them: only what the model is sent changes.
    kept = [m.text for m in result["messages"] if isinstance(m, ToolMessage)]
    assert kept == ["result " * 400] * 3


async def test_a_short_context_is_sent_whole() -> None:
    llm = ScriptedChatModel(responses=[AIMessage("ok")])
    graph = build_graph(Settings(), tools=[make_lookup_tool()], llm=llm)

    await graph.ainvoke({"messages": history(2)})

    assert CLEARED_TOOL_RESULT not in [m.text for m in llm.calls[0]]


# --- The web search cache ---


class FakeRedis:
    """Stands in for redis.asyncio.Redis: get and set, or `error` from both."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.error: Exception | None = None

    async def get(self, key: str) -> str | None:
        if self.error:
            raise self.error
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        if self.error:
            raise self.error
        self.data[key], self.ttls[key] = value, ex


class Searches:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.results: list[dict[str, Any]] | Exception = [RESULT]

    async def __call__(self, query: str, settings: Settings) -> list[dict[str, Any]]:
        self.queries.append(query)
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


RESULT = {"title": "REI Returns", "href": "https://rei.example/returns", "body": "30 days"}


@pytest.fixture
def searches(monkeypatch: pytest.MonkeyPatch) -> Searches:
    searches = Searches()
    monkeypatch.setattr(web_search_module, "search", searches)
    return searches


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


def cache(redis: FakeRedis) -> SearchCache:
    cache = SearchCache("redis://unused", seconds=600, max_results=5)
    cache.redis = redis  # type: ignore[assignment]
    return cache


async def search(tool: Any, query: str) -> Any:
    return await tool.ainvoke(
        {"type": "tool_call", "id": "1", "name": "web_search", "args": {"query": query}}
    )


async def test_a_repeated_query_is_answered_from_the_cache(
    searches: Searches, redis: FakeRedis
) -> None:
    tool = build_web_search_tool(Settings(), cache=cache(redis))

    first = await search(tool, "REI returns")
    second = await search(tool, "  rei   RETURNS ")

    assert searches.queries == ["REI returns"]
    assert second.content == first.content.replace("REI returns", "  rei   RETURNS ")
    assert second.artifact == first.artifact
    assert list(redis.ttls.values()) == [600]


async def test_failed_and_empty_searches_are_not_cached(
    searches: Searches, redis: FakeRedis
) -> None:
    tool = build_web_search_tool(Settings(), cache=cache(redis))

    searches.results = []
    await search(tool, "nothing")
    searches.results = RuntimeError("No results found.")
    with pytest.raises(RuntimeError):
        await search(tool, "rate limited")

    assert redis.data == {}


async def test_searches_go_on_while_redis_is_down(
    searches: Searches, redis: FakeRedis, caplog: pytest.LogCaptureFixture
) -> None:
    redis.error = RedisConnectionError("refused")
    tool = build_web_search_tool(Settings(), cache=cache(redis))

    first = await search(tool, "rei returns")
    await search(tool, "rei returns")

    assert "rei.example" in first.content
    assert searches.queries == ["rei returns", "rei returns"]
    assert caplog.text.count("web search cache unavailable") == 1


def test_no_cache_without_redis_or_with_a_ttl_of_zero() -> None:
    assert web_search_module.build_search_cache(Settings(redis_url=None)) is None
    no_ttl = Settings(redis_url="redis://localhost:6379/0", web_search_cache_seconds=0)
    assert web_search_module.build_search_cache(no_ttl) is None
