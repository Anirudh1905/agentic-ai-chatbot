import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fakes import ScriptedChatModel, make_lookup_tool, tool_call
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chatbot.api.dependencies import get_callbacks, get_graph
from chatbot.api.main import app
from chatbot.config import Settings
from chatbot.graph.builder import build_graph


@contextmanager
def make_client(reply: str) -> Iterator[TestClient]:
    llm = GenericFakeChatModel(messages=iter([AIMessage(reply)]))
    with client_for(build_graph(Settings(), llm=llm)) as c:
        yield c


@contextmanager
def client_for(graph: object) -> Iterator[TestClient]:
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_callbacks] = lambda: []
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client("hello from the bot") as c:
        yield c


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in body.replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_chat_returns_reply(client: TestClient) -> None:
    resp = client.post("/chat", json={"message": "hi", "thread_id": "t1"})

    assert resp.status_code == 200
    assert resp.json() == {"reply": "hello from the bot", "thread_id": "t1", "sources": []}


def test_chat_rejects_empty_message(client: TestClient) -> None:
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_stream_emits_tokens_then_done(client: TestClient) -> None:
    resp = client.post("/chat/stream", json={"message": "hi"})

    events = parse_sse(resp.text)
    assert events[-1] == ("done", {})
    tokens = [data["token"] for name, data in events if name == "token"]
    assert len(tokens) > 1
    assert "".join(tokens) == "hello from the bot"


def test_chat_stream_reports_errors() -> None:
    # An exhausted fake model raises, which should surface as an error event, not a 500.
    with make_client("only one reply") as c:
        c.post("/chat", json={"message": "first"})
        resp = c.post("/chat/stream", json={"message": "second"})

    events = parse_sse(resp.text)
    assert events[-1][0] == "error"


def tool_graph() -> object:
    llm = ScriptedChatModel(
        responses=[tool_call("lookup", query="refunds"), AIMessage("Five days [Doc › Section].")]
    )
    return build_graph(Settings(), tools=[make_lookup_tool()], llm=llm)


def test_chat_stream_emits_tool_events() -> None:
    with client_for(tool_graph()) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "refunds?"}).text)

    names = [name for name, _ in events]
    assert names.index("tool_start") < names.index("tool_end") < names.index("token")
    start = dict(events)["tool_start"]
    end = dict(events)["tool_end"]
    assert start["name"] == "lookup" and start["input"] == {"query": "refunds"}
    assert start["id"] == end["id"]
    assert end["sources"] == [{"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}]
    assert (
        "".join(str(d["token"]) for n, d in events if n == "token") == "Five days [Doc › Section]."
    )


def test_chat_returns_sources_from_tools() -> None:
    with client_for(tool_graph()) as c:
        body = c.post("/chat", json={"message": "refunds?"}).json()

    assert body["reply"] == "Five days [Doc › Section]."
    assert body["sources"] == [{"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}]
