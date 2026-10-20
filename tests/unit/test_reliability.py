"""Circuit breakers and the fallback model (Phase 9, chatbot/reliability/)."""

import logging
from typing import Any

import pytest
from apiclient import client_for, parse_sse, scripted
from fakes import FactMemoryModel, FailingChatModel, outage
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.store.memory import InMemoryStore
from prometheus_client import REGISTRY

from chatbot.config import Settings
from chatbot.memory.long_term import MemoryWriter
from chatbot.reliability import breaker as breaker_module
from chatbot.reliability.breaker import (
    BreakerOpen,
    Breakers,
    Permit,
    RedisBreakerStore,
    SharedBreakerStore,
)
from chatbot.storage.embeddings import GuardedEmbeddings
from chatbot.tools import web_search
from chatbot.tools.web_search import UNAVAILABLE, build_web_search_tool

SETTINGS = Settings(breaker_failures=3, breaker_window_seconds=60, breaker_cooldown_seconds=30)


class Clock:
    """Stands in for time.monotonic in the breaker module, so tests can move time on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr(breaker_module.time, "monotonic", lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def breaker_open(name: str) -> float | None:
    return REGISTRY.get_sample_value("chatbot_breaker_open", {"dependency": name})


async def fail(breakers: Breakers, name: str, times: int) -> None:
    for _ in range(times):
        with pytest.raises(Exception):  # noqa: B017 - the outage, or BreakerOpen once it trips
            await breakers.get(name).call(lambda: raise_(outage()))


async def raise_(error: Exception) -> None:
    raise error


async def ok() -> str:
    return "ok"


# The breaker


async def test_failures_open_the_breaker_then_calls_fail_fast(monkeypatch: Any) -> None:
    Clock(monkeypatch)
    breakers = Breakers(SETTINGS)
    await fail(breakers, "model-a", 3)
    calls = []

    async def call() -> str:
        calls.append(1)
        return "ok"

    with pytest.raises(BreakerOpen):
        await breakers.get("model-a").call(call)
    assert calls == []
    assert breaker_open("model-a") == 1
    assert await breakers.get("model-b").call(ok) == "ok"  # one breaker per dependency


async def test_failures_spread_beyond_the_window_dont_open_it(monkeypatch: Any) -> None:
    clock = Clock(monkeypatch)
    breakers = Breakers(SETTINGS)
    await fail(breakers, "model", 2)
    clock.advance(61)
    await fail(breakers, "model", 2)

    assert await breakers.get("model").call(ok) == "ok"


async def test_refused_requests_are_not_outages(monkeypatch: Any) -> None:
    Clock(monkeypatch)
    breakers = Breakers(SETTINGS)
    for _ in range(5):
        with pytest.raises(Exception, match="bad_request"):
            await breakers.get("model").call(lambda: raise_(outage("bad_request")))

    assert await breakers.get("model").call(ok) == "ok"


async def test_after_the_cooldown_one_call_probes_and_its_success_closes_it(
    monkeypatch: Any,
) -> None:
    clock = Clock(monkeypatch)
    breakers = Breakers(SETTINGS)
    breaker = breakers.get("model")
    await fail(breakers, "model", 3)
    clock.advance(31)

    probe = await breaker.allow()
    others = await breaker.allow()  # while the probe is out
    assert probe is Permit.PROBE
    assert others is None
    await breaker.succeeded(probe)

    assert await breaker.allow() is Permit.CLOSED
    assert breaker_open("model") == 0


async def test_a_failed_probe_opens_it_again(monkeypatch: Any) -> None:
    clock = Clock(monkeypatch)
    breakers = Breakers(SETTINGS)
    breaker = breakers.get("model")
    await fail(breakers, "model", 3)
    clock.advance(31)

    probe = await breaker.allow()
    assert probe is Permit.PROBE
    await breaker.failed(probe)

    assert await breaker.allow() is None
    clock.advance(31)
    assert await breaker.allow() is Permit.PROBE


async def test_a_probe_whose_pod_died_lets_another_call_probe(monkeypatch: Any) -> None:
    clock = Clock(monkeypatch)
    breakers = Breakers(SETTINGS)
    breaker = breakers.get("model")
    await fail(breakers, "model", 3)
    clock.advance(31)
    assert await breaker.allow() is Permit.PROBE  # and never reports back

    clock.advance(breaker_module.PROBE_SECONDS + 1)
    assert await breaker.allow() is Permit.PROBE


async def test_without_redis_each_process_keeps_its_breakers_in_memory(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    Clock(monkeypatch)
    # Nothing listens on port 1: every Redis call fails at once.
    breakers = Breakers(SETTINGS, SharedBreakerStore(RedisBreakerStore("redis://127.0.0.1:1/0")))
    with caplog.at_level(logging.WARNING, "chatbot.reliability.breaker"):
        await fail(breakers, "model", 3)
        with pytest.raises(BreakerOpen):
            await breakers.get("model").call(ok)

    unreachable = [r for r in caplog.records if "can't reach Redis" in r.message]
    assert len(unreachable) == 1  # logged once, not on every call


# The agent's model calls: ModelRetryMiddleware, ModelBreaker, ModelFallbackMiddleware


def test_an_outage_of_the_model_is_retried_then_falls_back() -> None:
    primary = FailingChatModel()
    with client_for(primary, fallback=scripted("The backup answers.")) as c:
        resp = c.post("/chat", json={"message": "hi"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "The backup answers."
    assert primary.calls == 2  # the call and one retry (MODEL_MAX_RETRIES)


def test_a_refused_request_is_not_retried_or_counted() -> None:
    primary = FailingChatModel(error=outage("bad_request"))
    breakers = Breakers(SETTINGS)
    with client_for(primary, fallback=scripted(*["Backup."] * 4), breakers=breakers) as c:
        for _ in range(4):
            c.post("/chat", json={"message": "hi"})

    # ModelFallbackMiddleware still asks the fallback, but the breaker never opened.
    assert primary.calls == 4


def test_an_open_breaker_skips_the_model() -> None:
    primary = FailingChatModel()
    backup = scripted(*[f"Backup {i}." for i in range(4)])
    breakers = Breakers(SETTINGS)
    with client_for(primary, fallback=backup, breakers=breakers, model_max_retries=0) as c:
        for i in range(4):
            assert c.post("/chat", json={"message": f"q{i}"}).json()["reply"] == f"Backup {i}."

    # Three outages opened it; the fourth question went straight to the backup.
    assert primary.calls == 3
    assert breaker_open("primary-model") == 1


def test_a_reply_that_failed_partway_starts_over_on_the_fallback() -> None:
    # Longer than the PII redactor holds back (StreamRedactor.LOOKBACK), so some of it shows.
    partial = "Our refund window is thirty days from delivery for most items. " * 4
    primary = FailingChatModel(partial=partial)
    with client_for(
        primary, fallback=scripted("Refunds take five days."), model_max_retries=0
    ) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "refunds?"}).text)

    kinds = [name for name, _ in events]
    reset = kinds.index("reset")
    before = "".join(d["token"] for name, d in events[:reset] if name == "token")
    after = "".join(d["token"] for name, d in events[reset:] if name == "token")
    assert before and partial.startswith(before)
    assert after == "Refunds take five days."
    assert kinds.count("reset") == 1
    assert kinds[-1] == "done"


def test_no_reset_when_nothing_was_shown_yet() -> None:
    with client_for(FailingChatModel(), fallback=scripted("Backup."), model_max_retries=0) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "hi"}).text)

    assert "reset" not in [name for name, _ in events]
    assert events[-1][0] == "done"


def test_a_user_on_their_own_key_never_falls_back_to_ours() -> None:
    backup = scripted("never used")
    # Their endpoint: nothing listens there, an outage.
    headers = {"X-OpenAI-Key": "sk-theirs", "X-OpenAI-Base-URL": "http://127.0.0.1:9/v1"}
    with client_for(scripted("unused"), fallback=backup, model_max_retries=0) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "hi"}, headers=headers).text)

    # The fallback's call goes to their endpoint too (UserModel), never to our key.
    assert events[-1][0] == "error"
    assert backup.calls == []


# Memory pushes, embeddings, web search


async def test_memory_pushes_fall_back_too() -> None:
    class Down(FailingChatModel):
        def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
            async def run(_: Any) -> Any:
                raise outage()

            return RunnableLambda(run)

    writer = MemoryWriter(Down(), fallback=FactMemoryModel())

    facts = await writer.extract.ainvoke([HumanMessage("fact: city=Lisbon")])

    assert len(facts.memories) == 1  # from the fallback


async def test_an_open_embeddings_breaker_fails_fast(monkeypatch: Any) -> None:
    Clock(monkeypatch)

    class Down(Embeddings):
        calls = 0

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise NotImplementedError

        def embed_query(self, text: str) -> list[float]:
            raise NotImplementedError

        async def aembed_query(self, text: str) -> list[float]:
            Down.calls += 1
            raise outage("server_error")

    embeddings = GuardedEmbeddings(Down(), Breakers(SETTINGS).get("embeddings"))
    for _ in range(3):
        with pytest.raises(Exception, match="server_error"):
            await embeddings.aembed_query("refunds")

    with pytest.raises(BreakerOpen):
        await embeddings.aembed_query("refunds")
    assert Down.calls == 3


async def test_web_search_answers_unavailable_while_its_breaker_is_open(
    monkeypatch: Any,
) -> None:
    Clock(monkeypatch)
    searched = []

    async def search(query: str, settings: Settings) -> list[dict[str, Any]]:
        searched.append(query)
        raise RuntimeError("No results found.")

    monkeypatch.setattr(web_search, "search", search)
    breakers = Breakers(SETTINGS)
    tool = build_web_search_tool(SETTINGS, breakers)
    assert tool is not None
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await tool.ainvoke({"query": "weather"})

    result = await tool.ainvoke({"query": "weather"})

    assert result == UNAVAILABLE
    assert len(searched) == 3


def test_a_turn_goes_on_without_recall_when_the_embeddings_are_down() -> None:
    class EmbeddingsDown(InMemoryStore):
        async def asearch(self, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("query"):
                raise BreakerOpen("embeddings")
            return await super().asearch(*args, **kwargs)

    with client_for(scripted(AIMessage("Hello.")), store=EmbeddingsDown()) as c:
        resp = c.post("/chat", json={"message": "hi"})

    assert resp.json()["reply"] == "Hello."
