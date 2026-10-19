import asyncio
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from apiclient import AUTH, BOB, client_for, parse_sse, scripted
from fakes import (
    Crash,
    ScriptedChatModel,
    make_flaky_lookup_tool,
    make_lookup_tool,
    tool_call,
)
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from prometheus_client import REGISTRY
from pydantic import SecretStr

from chatbot.api.dependencies import (
    get_callbacks,
    get_checkpointer,
    get_graph,
)
from chatbot.api.main import app
from chatbot.config import ModelCredentials, RunContext, get_settings
from chatbot.observability.logs import ContextFilter
from chatbot.observability.tracing import hashed_user, trace_metadata

SOURCE = {"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}


def tool_llm() -> ScriptedChatModel:
    return scripted(tool_call("lookup", query="refunds"), "Five days [Doc › Section].")


@pytest.fixture
def client() -> Iterator[TestClient]:
    llm = GenericFakeChatModel(messages=iter([AIMessage("hello from the bot")]))
    with client_for(llm) as c:
        yield c


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_chat_returns_reply(client: TestClient) -> None:
    thread_id = str(uuid.uuid4())

    resp = client.post("/chat", json={"message": "hi", "thread_id": thread_id})

    assert resp.status_code == 200
    assert resp.json() == {
        "reply": "hello from the bot",
        "thread_id": thread_id,
        "sources": [],
        "pending": None,
    }


def test_chat_without_thread_id_starts_a_new_thread(client: TestClient) -> None:
    body = client.post("/chat", json={"message": "hi"}).json()

    threads = client.get("/threads").json()
    assert [t["id"] for t in threads] == [body["thread_id"]]
    assert threads[0]["title"] == "hi"


def test_chat_rejects_empty_message(client: TestClient) -> None:
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_rejects_malformed_thread_id(client: TestClient) -> None:
    assert client.post("/chat", json={"message": "hi", "thread_id": "t1"}).status_code == 422


def test_requests_without_a_token_are_rejected(client: TestClient) -> None:
    headers = {"Authorization": ""}
    assert client.post("/chat", json={"message": "hi"}, headers=headers).status_code == 401
    assert client.get("/threads", headers=headers).status_code == 401


def test_chat_stream_emits_tokens_then_done(client: TestClient) -> None:
    resp = client.post("/chat/stream", json={"message": "hi"})

    events = parse_sse(resp.text)
    assert events[-1][0] == "done" and events[-1][1]["pending"] is False
    uuid.UUID(str(events[-1][1]["thread_id"]))
    tokens = [data["token"] for name, data in events if name == "token"]
    assert "".join(tokens) == "hello from the bot"


def test_long_replies_stream_in_pieces() -> None:
    # PII redaction holds back the last 128 characters; anything before that streams.
    reply = " ".join(f"word{i}" for i in range(80))
    with client_for(scripted(reply)) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "hi"}).text)

    tokens = [str(data["token"]) for name, data in events if name == "token"]
    assert len(tokens) > 10
    assert "".join(tokens) == reply


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
    with client_for(llm, context_max_tokens=60) as c:
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
        before_answer = next(h for h in history if h["next"] and h["message_count"] == 3)

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

    assert body == {"thread_id": thread_id, "summary": None, "messages": [], "pending": None}


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
        line.startswith(f"chatbot.agent.middleware [req=abc123 thread={thread}]: model replied")
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


def test_thread_title_is_redacted_and_logged_without_values(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    client.post("/chat", json={"message": "hi, I'm jane@example.com"})

    assert client.get("/threads").json()[0]["title"] == "hi, I'm [REDACTED_EMAIL]"
    assert "redacted PII from the message: email" in caplog.text
    assert "jane@example.com" not in caplog.text


def test_several_tool_rounds_fit_in_the_step_limit() -> None:
    # Each round is many graph steps (one per middleware hook); LangGraph's default limit of
    # 25 used to end a run like this with GraphRecursionError.
    llm = scripted(
        tool_call("lookup", call_id="a", query="one"),
        tool_call("lookup", call_id="b", query="two"),
        tool_call("lookup", call_id="c", query="three"),
        "Done after three searches.",
    )
    with client_for(llm, [make_lookup_tool()]) as c:
        events = parse_sse(c.post("/chat/stream", json={"message": "dig deep"}).text)

    assert events[-1][0] == "done"
    assert (
        "".join(str(d["token"]) for n, d in events if n == "token") == "Done after three searches."
    )


class RootMetadata(BaseCallbackHandler):
    """Records each run's metadata, where Langfuse reads the trace's attributes."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def on_chain_start(self, *args: Any, parent_run_id: Any = None, **kwargs: Any) -> None:
        if parent_run_id is None:
            self.seen.append(kwargs.get("metadata") or {})


def test_traces_carry_the_thread_and_a_hashed_user() -> None:
    llm = GenericFakeChatModel(messages=iter([AIMessage("hi"), AIMessage("again")]))
    recorder = RootMetadata()
    with client_for(llm) as client:
        app.dependency_overrides[get_callbacks] = lambda: [recorder]
        thread = client.post("/chat", json={"message": "hello"}).json()["thread_id"]
        client.post("/chat", json={"message": "more", "thread_id": thread})

    first, second = recorder.seen
    assert first["langfuse_session_id"] == second["langfuse_session_id"] == thread
    assert first["langfuse_user_id"] == hashed_user("alice", get_settings()) != "alice"
    assert first["langfuse_tags"] == ["chat"]


def test_a_run_on_the_users_own_key_is_tagged() -> None:
    own = RunContext("alice", ModelCredentials("https://llm.example/v1", SecretStr("sk")))
    assert trace_metadata("resume", uuid.uuid4(), own, AUTH)["langfuse_tags"] == [
        "resume",
        "own-key",
    ]


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_count_requests_tools_tokens_and_first_tokens() -> None:
    llm = scripted(
        tool_call("lookup", query="refunds"),
        AIMessage(
            "Five days.", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
        ),
    )
    route = {"method": "POST", "route": "/chat/stream"}
    before = {
        "requests": sample("chatbot_http_requests_total", **route, status="200"),
        "tool": sample("chatbot_tool_calls_total", tool="lookup", outcome="ok"),
        "ttft": sample("chatbot_time_to_first_token_seconds_count"),
        "recalls": sample("chatbot_memory_recalls_total", result="miss"),
    }
    with client_for(llm, tools=[make_lookup_tool()]) as client:
        client.post("/chat/stream", json={"message": "refunds?"})
        exposed = client.get("/metrics").text

    assert sample("chatbot_http_requests_total", **route, status="200") == before["requests"] + 1
    assert sample("chatbot_tool_calls_total", tool="lookup", outcome="ok") == before["tool"] + 1
    assert sample("chatbot_time_to_first_token_seconds_count") == before["ttft"] + 1
    assert sample("chatbot_memory_recalls_total", result="miss") == before["recalls"]  # no store
    assert "chatbot_model_tokens_total" in exposed
    assert 'route="/chat/stream"' in exposed  # the template, not the path
