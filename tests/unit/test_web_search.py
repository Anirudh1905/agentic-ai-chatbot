from typing import Any

import pytest

from chatbot.config import Settings
from chatbot.tools import web_search as web_search_module
from chatbot.tools.web_search import build_web_search_tool, format_results


class FakeDDGS:
    """Stands in for ddgs.DDGS. `responses` maps backend -> results list or exception."""

    responses: dict[str, Any] = {}
    calls: list[str] = []

    def __init__(self, timeout: int) -> None:
        pass

    def text(self, query: str, max_results: int, backend: str) -> list[dict[str, Any]]:
        FakeDDGS.calls.append(backend)
        response = FakeDDGS.responses[backend]
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def fake_ddgs(monkeypatch: pytest.MonkeyPatch) -> type[FakeDDGS]:
    FakeDDGS.responses, FakeDDGS.calls = {}, []
    monkeypatch.setattr(web_search_module, "DDGS", FakeDDGS)
    monkeypatch.setattr(web_search_module, "RETRY_DELAY_SECONDS", 0)
    return FakeDDGS


RESULT = {"title": "REI Returns", "href": "https://rei.example/returns", "body": "x" * 2000}
AD = {"title": "Ad", "href": "https://www.bing.com/aclick?ld=abc", "body": "buy now"}


async def run(query: str = "rei returns") -> Any:
    tool = build_web_search_tool(Settings())
    assert tool is not None
    return await tool.ainvoke(
        {"type": "tool_call", "id": "1", "name": "web_search", "args": {"query": query}}
    )


def test_can_be_disabled() -> None:
    assert build_web_search_tool(Settings(web_search_enabled=False)) is None


async def test_returns_trimmed_results_and_sources(fake_ddgs: type[FakeDDGS]) -> None:
    fake_ddgs.responses = {"duckduckgo": [RESULT]}

    message = await run()

    assert fake_ddgs.calls == ["duckduckgo"]
    assert '<result cite_as="[REI Returns](https://rei.example/returns)">' in message.content
    assert len(message.content) < 1200
    assert message.artifact == [
        {
            "type": "web",
            "title": "REI Returns",
            "url": "https://rei.example/returns",
            "cite_as": "[REI Returns](https://rei.example/returns)",
        }
    ]


async def test_drops_ad_links(fake_ddgs: type[FakeDDGS]) -> None:
    fake_ddgs.responses = {"duckduckgo": [AD, RESULT]}

    message = await run()

    assert [s["title"] for s in message.artifact] == ["REI Returns"]


async def test_falls_back_to_auto_when_duckduckgo_fails(fake_ddgs: type[FakeDDGS]) -> None:
    fake_ddgs.responses = {"duckduckgo": RuntimeError("No results found."), "auto": [RESULT]}

    message = await run()

    assert fake_ddgs.calls == ["duckduckgo", "auto"]
    assert "REI Returns" in message.content


async def test_falls_back_when_duckduckgo_returns_only_ads(fake_ddgs: type[FakeDDGS]) -> None:
    fake_ddgs.responses = {"duckduckgo": [AD], "auto": [RESULT]}

    message = await run()

    assert fake_ddgs.calls == ["duckduckgo", "auto"]
    assert "REI Returns" in message.content


async def test_reports_failure_when_every_backend_fails(fake_ddgs: type[FakeDDGS]) -> None:
    fake_ddgs.responses = {"duckduckgo": TimeoutError(), "auto": TimeoutError()}

    message = await run()

    assert fake_ddgs.calls == ["duckduckgo", "auto", "duckduckgo", "auto"]
    assert "unavailable" in message.content
    assert message.artifact == []


async def test_no_results_is_not_an_error(fake_ddgs: type[FakeDDGS]) -> None:
    fake_ddgs.responses = {"duckduckgo": [], "auto": []}

    message = await run("zzqx nonsense")

    assert "returned no results" in message.content


def test_format_results_empty() -> None:
    assert "no results" in format_results("q", [])


async def test_retries_after_a_delay_when_rate_limited(fake_ddgs: type[FakeDDGS]) -> None:
    class Flaky(FakeDDGS):
        def text(self, query: str, max_results: int, backend: str) -> list[dict[str, Any]]:
            FakeDDGS.calls.append(backend)
            if len(FakeDDGS.calls) <= 2:
                raise RuntimeError("No results found.")
            return [RESULT]

    web_search_module.DDGS = Flaky  # type: ignore[misc]

    message = await run()

    assert FakeDDGS.calls == ["duckduckgo", "auto", "duckduckgo"]
    assert "REI Returns" in message.content
