import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fakes import (
    Crash,
    InMemoryThreadStore,
    ScriptedChatModel,
    make_flaky_lookup_tool,
    make_lookup_tool,
    tool_call,
)
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from chatbot.api.dependencies import (
    get_callbacks,
    get_checkpointer,
    get_graph,
    get_threads,
)
from chatbot.api.main import app
from chatbot.config import Settings
from chatbot.graph.builder import build_graph
from chatbot.observability import ContextFilter

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}
SOURCE = {"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}


@contextmanager
def client_for(
    llm: BaseChatModel, tools: list[BaseTool] | None = None, **settings: Any
) -> Iterator[TestClient]:
    checkpointer = InMemorySaver()
    graph = build_graph(Settings(**settings), tools=tools or [], llm=llm, checkpointer=checkpointer)
    threads = InMemoryThreadStore()
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    app.dependency_overrides[get_threads] = lambda: threads
    app.dependency_overrides[get_callbacks] = lambda: []
    try:
        with TestClient(app, headers=ALICE) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def scripted(*replies: AIMessage | str) -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[r if isinstance(r, AIMessage) else AIMessage(r) for r in replies]
    )


def tool_llm() -> ScriptedChatModel:
    return scripted(tool_call("lookup", query="refunds"), "Five days [Doc › Section].")


@pytest.fixture
def client() -> Iterator[TestClient]:
    llm = GenericFakeChatModel(messages=iter([AIMessage("hello from the bot")]))
    with client_for(llm) as c:
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
    thread_id = str(uuid.uuid4())

    resp = client.post("/chat", json={"message": "hi", "thread_id": thread_id})

    assert resp.status_code == 200
    assert resp.json() == {"reply": "hello from the bot", "thread_id": thread_id, "sources": []}


def test_chat_without_thread_id_starts_a_new_thread(client: TestClient) -> None:
    body = client.post("/chat", json={"message": "hi"}).json()

    threads = client.get("/threads").json()
    assert [t["id"] for t in threads] == [body["thread_id"]]
    assert threads[0]["title"] == "hi"


def test_chat_rejects_empty_message(client: TestClient) -> None:
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_rejects_malformed_thread_id(client: TestClient) -> None:
    assert client.post("/chat", json={"message": "hi", "thread_id": "t1"}).status_code == 422


def test_requests_without_a_user_are_rejected(client: TestClient) -> None:
    headers = {"X-User-Id": ""}
    assert client.post("/chat", json={"message": "hi"}, headers=headers).status_code == 401
    assert client.get("/threads", headers=headers).status_code == 401


def test_chat_stream_emits_tokens_then_done(client: TestClient) -> None:
    resp = client.post("/chat/stream", json={"message": "hi"})

    events = parse_sse(resp.text)
    assert events[-1][0] == "done"
    uuid.UUID(str(events[-1][1]["thread_id"]))
    tokens = [data["token"] for name, data in events if name == "token"]
    assert len(tokens) > 1
    assert "".join(tokens) == "hello from the bot"


def test_chat_stream_reports_errors() -> None:
    # An exhausted fake model raises, which should surface as an error event, not a 500.
    with client_for(scripted("only one reply")) as c:
        c.post("/chat", json={"message": "first"})
        resp = c.post("/chat/stream", json={"message": "second"})

    events = parse_sse(resp.text)
    assert events[-1][0] == "error"


def test_chat_stream_emits_tool_events() -> None:
    with client_for(tool_llm(), [make_lookup_tool()]) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "refunds?"}).text)

    names = [name for name, _ in events]
    assert names.index("tool_start") < names.index("tool_end") < names.index("token")
    start = dict(events)["tool_start"]
    end = dict(events)["tool_end"]
    assert start["name"] == "lookup" and start["input"] == {"query": "refunds"}
    assert start["id"] == end["id"]
    assert end["sources"] == [SOURCE]
    assert (
        "".join(str(d["token"]) for n, d in events if n == "token") == "Five days [Doc › Section]."
    )


def test_chat_stream_does_not_stream_the_summary() -> None:
    llm = scripted("first " * 50, "SUMMARY", "second reply")
    with client_for(llm, context_max_tokens=60, context_keep_turns=1) as c:
        thread_id = c.post("/chat", json={"message": "one"}).json()["thread_id"]
        resp = c.post("/chat/stream", json={"message": "two", "thread_id": thread_id})
        messages = c.get(f"/threads/{thread_id}/messages").json()

    tokens = "".join(str(d["token"]) for n, d in parse_sse(resp.text) if n == "token")
    assert tokens == "second reply"
    assert messages["summary"] == "SUMMARY"
    assert [m["content"] for m in messages["messages"]] == ["two", "second reply"]


def test_chat_returns_sources_from_tools() -> None:
    with client_for(tool_llm(), [make_lookup_tool()]) as c:
        body = c.post("/chat", json={"message": "refunds?"}).json()

    assert body["reply"] == "Five days [Doc › Section]."
    assert body["sources"] == [SOURCE]


def test_sources_are_only_from_the_current_turn() -> None:
    llm = scripted(tool_call("lookup", query="refunds"), "Five days.", "You're welcome.")
    with client_for(llm, [make_lookup_tool()]) as c:
        thread_id = c.post("/chat", json={"message": "refunds?"}).json()["thread_id"]
        body = c.post("/chat", json={"message": "thanks", "thread_id": thread_id}).json()

    assert body["sources"] == []


def test_thread_continues_across_requests() -> None:
    llm = scripted("Hi Sam.", "Your name is Sam.")
    with client_for(llm) as c:
        thread_id = c.post("/chat", json={"message": "I'm Sam"}).json()["thread_id"]
        c.post("/chat", json={"message": "What's my name?", "thread_id": thread_id})

    assert [m.text for m in llm.calls[1][1:]] == ["I'm Sam", "Hi Sam.", "What's my name?"]


def test_thread_messages_fold_tool_activity_into_replies() -> None:
    with client_for(tool_llm(), [make_lookup_tool()]) as c:
        thread_id = c.post("/chat", json={"message": "refunds?"}).json()["thread_id"]
        body = c.get(f"/threads/{thread_id}/messages").json()

    assert body["summary"] is None
    assert body["messages"] == [
        {"role": "user", "content": "refunds?", "tools": [], "sources": []},
        {
            "role": "assistant",
            "content": "Five days [Doc › Section].",
            "tools": ["lookup"],
            "sources": [SOURCE],
        },
    ]


def test_create_and_list_threads(client: TestClient) -> None:
    created = client.post("/threads", json={"title": "Trip planning"})
    client.post("/threads")
    client.post("/threads", headers=BOB)

    assert created.status_code == 201
    mine = client.get("/threads").json()
    assert len(mine) == 2
    assert created.json()["id"] in {t["id"] for t in mine}
    assert len(client.get("/threads", headers=BOB).json()) == 1


def test_users_cannot_touch_other_users_threads(client: TestClient) -> None:
    thread_id = client.post("/chat", json={"message": "secret"}).json()["thread_id"]

    def as_bob(method: str, path: str, **kwargs: Any) -> int:
        return client.request(method, path, headers=BOB, **kwargs).status_code

    assert as_bob("POST", "/chat", json={"message": "hi", "thread_id": thread_id}) == 404
    assert as_bob("POST", "/chat/stream", json={"message": "hi", "thread_id": thread_id}) == 404
    assert as_bob("GET", f"/threads/{thread_id}/messages") == 404
    assert as_bob("GET", f"/threads/{thread_id}/history") == 404
    assert as_bob("POST", f"/threads/{thread_id}/resume") == 404
    assert as_bob("DELETE", f"/threads/{thread_id}") == 404
    # Alice's thread is untouched.
    assert len(client.get(f"/threads/{thread_id}/messages").json()["messages"]) == 2


def test_delete_thread_removes_its_checkpoints() -> None:
    with client_for(scripted("hi")) as c:
        thread_id = c.post("/chat", json={"message": "hello"}).json()["thread_id"]
        checkpointer = app.dependency_overrides[get_checkpointer]()

        assert c.delete(f"/threads/{thread_id}").status_code == 204

        assert c.get("/threads").json() == []
        assert c.get(f"/threads/{thread_id}/messages").status_code == 404
        assert list(checkpointer.list({"configurable": {"thread_id": thread_id}})) == []


def test_history_lists_checkpoints_newest_first() -> None:
    with client_for(tool_llm(), [make_lookup_tool()]) as c:
        thread_id = c.post("/chat", json={"message": "refunds?"}).json()["thread_id"]
        history = c.get(f"/threads/{thread_id}/history").json()

    assert history[0]["next"] == [] and history[0]["message_count"] == 4
    assert history[0]["last_message"] == "ai: Five days [Doc › Section]."
    steps = [h["step"] for h in history]
    assert steps == sorted(steps, reverse=True)
    assert history[0]["parent_checkpoint_id"] == history[1]["checkpoint_id"]


def test_resume_finishes_a_run_that_crashed() -> None:
    with client_for(tool_llm(), [make_flaky_lookup_tool()]) as c:
        thread_id = c.post("/threads").json()["id"]
        # Crash outside the test client: a BaseException would take its event loop down too.
        graph = app.dependency_overrides[get_graph]()
        config = {"configurable": {"thread_id": thread_id}}
        with pytest.raises(Crash):
            asyncio.run(graph.ainvoke({"messages": [HumanMessage("refunds?")]}, config))

        resp = c.post(f"/threads/{thread_id}/resume")
        again = c.post(f"/threads/{thread_id}/resume")

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Five days [Doc › Section]."
    assert resp.json()["sources"] == [SOURCE]
    assert again.status_code == 409


def test_resume_from_an_earlier_checkpoint_forks_the_thread() -> None:
    llm = scripted(tool_call("lookup", query="refunds"), "First answer.", "Second answer.")
    with client_for(llm, [make_lookup_tool()]) as c:
        thread_id = c.post("/chat", json={"message": "refunds?"}).json()["thread_id"]
        history = c.get(f"/threads/{thread_id}/history").json()
        # The checkpoint right after the tool ran, before the final answer.
        before_answer = next(h for h in history if h["next"] == ["llm"] and h["message_count"] == 3)

        resp = c.post(
            f"/threads/{thread_id}/resume", json={"checkpoint_id": before_answer["checkpoint_id"]}
        )
        messages = c.get(f"/threads/{thread_id}/messages").json()["messages"]
        unknown = c.post(f"/threads/{thread_id}/resume", json={"checkpoint_id": "nope"})

    assert resp.json()["reply"] == "Second answer."
    assert [m["content"] for m in messages] == ["refunds?", "Second answer."]
    assert unknown.status_code == 404


def test_messages_of_an_empty_thread(client: TestClient) -> None:
    thread_id = client.post("/threads").json()["id"]

    body = client.get(f"/threads/{thread_id}/messages").json()

    assert body == {"thread_id": thread_id, "summary": None, "messages": []}


def test_requests_are_logged_with_request_and_thread_ids(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    caplog.handler.addFilter(ContextFilter())

    resp = client.post("/chat", json={"message": "hi"}, headers={"X-Request-Id": "abc123"})

    assert resp.headers["x-request-id"] == "abc123"
    thread = resp.json()["thread_id"][:8]
    lines = [f"{r.name}{r.ctx}: {r.getMessage()}" for r in caplog.records]  # type: ignore[attr-defined]
    assert any(
        line.startswith(f"chatbot.graph.builder [req=abc123 thread={thread}]: model replied")
        for line in lines
    )
    assert any(
        line.startswith(f"chatbot.api [req=abc123 thread={thread}]: POST /chat -> 200")
        and "user=alice" in line
        for line in lines
    )


def test_health_probes_are_not_logged_at_info(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    client.get("/healthz")
    assert not [r for r in caplog.records if "/healthz" in r.getMessage()]
