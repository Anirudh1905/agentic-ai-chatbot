from collections.abc import Iterator
from contextlib import AbstractContextManager

import pytest
from apiclient import BOB, client_for, parse_sse, scripted
from fakes import make_lookup_tool, tool_call
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool

from chatbot.hitl.policy import interrupt_on

REASON = 'The query mentions "internal", which may be internal information.'


def web_search_tool(seen: list[str]) -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def web_search(query: str) -> tuple[str, list[dict[str, str]]]:
        """Search the web."""
        seen.append(query)
        return f"web results for {query}", [{"type": "web", "cite_as": f"[{query}](u)"}]

    return web_search


@pytest.fixture
def seen() -> list[str]:
    return []


def hitl_client(llm: BaseChatModel, seen: list[str]) -> AbstractContextManager[TestClient]:
    tools = [web_search_tool(seen), make_lookup_tool()]
    rules = interrupt_on(tools, ["internal"]) | {"lookup": False}
    return client_for(llm, tools, rules)


@pytest.fixture
def client(seen: list[str]) -> Iterator[TestClient]:
    llm = scripted(tool_call("web_search", query="internal roadmap"), "Here you go.")
    with hitl_client(llm, seen) as c:
        yield c


def start(client: TestClient) -> str:
    body = client.post("/chat", json={"message": "find the roadmap"}).json()
    assert body["reply"] == "" and body["pending"] is not None
    return str(body["thread_id"])


def test_chat_returns_the_pending_request(client: TestClient, seen: list[str]) -> None:
    body = client.post("/chat", json={"message": "find the roadmap"}).json()

    assert body["pending"] == {
        "action_requests": [
            {"name": "web_search", "args": {"query": "internal roadmap"}, "description": REASON}
        ],
        "review_configs": [
            {"action_name": "web_search", "allowed_decisions": ["approve", "edit", "reject"]}
        ],
    }
    assert seen == []
    thread_id = body["thread_id"]
    assert client.get(f"/threads/{thread_id}/messages").json()["pending"] == body["pending"]


def test_stream_emits_interrupt_then_done(client: TestClient) -> None:
    events = parse_sse(client.post("/chat/stream", json={"message": "find it"}).text)

    names = [n for n, _ in events]
    assert names[-2:] == ["interrupt", "done"]
    assert "token" not in names and "tool_start" not in names
    assert events[-2][1]["action_requests"][0]["description"] == REASON
    assert events[-1][1]["pending"] is True


def test_new_message_while_waiting_is_rejected(client: TestClient) -> None:
    thread_id = start(client)

    resp = client.post("/chat", json={"message": "hello?", "thread_id": thread_id})

    assert resp.status_code == 409


def test_resume_needs_one_allowed_decision_per_call(client: TestClient) -> None:
    thread_id = start(client)
    url = f"/threads/{thread_id}/resume"

    assert client.post(url).status_code == 409
    too_many = client.post(url, json={"decisions": [{"type": "approve"}, {"type": "approve"}]})
    assert too_many.status_code == 422
    assert client.post(url, json={"decisions": [{"type": "edit"}]}).status_code == 422
    assert client.post(url, json={"decisions": [{"type": "maybe"}]}).status_code == 422
    # Still waiting after all that.
    assert client.get(f"/threads/{thread_id}/messages").json()["pending"] is not None


def test_approve(client: TestClient, seen: list[str]) -> None:
    thread_id = start(client)

    body = client.post(
        f"/threads/{thread_id}/resume", json={"decisions": [{"type": "approve"}]}
    ).json()

    assert body["reply"] == "Here you go." and body["pending"] is None
    assert body["sources"] == [{"type": "web", "cite_as": "[internal roadmap](u)"}]
    assert seen == ["internal roadmap"]
    assert client.get(f"/threads/{thread_id}/messages").json()["pending"] is None


def test_edit_streams_the_edited_search(client: TestClient, seen: list[str]) -> None:
    thread_id = start(client)
    edit = {"type": "edit", "edited_action": {"name": "web_search", "args": {"query": "gear"}}}

    resp = client.post(f"/threads/{thread_id}/resume/stream", json={"decisions": [edit]})

    events = parse_sse(resp.text)
    assert dict(events)["tool_start"]["input"] == {"query": "gear"}
    assert "".join(str(d["token"]) for n, d in events if n == "token") == "Here you go."
    assert events[-1] == ("done", {"thread_id": thread_id, "pending": False})
    assert seen == ["gear"]


def test_reject(client: TestClient, seen: list[str]) -> None:
    thread_id = start(client)

    decision = {"type": "reject", "message": "confidential"}
    body = client.post(f"/threads/{thread_id}/resume", json={"decisions": [decision]}).json()

    assert body["reply"] == "Here you go."
    assert seen == []


def test_mixed_decisions_through_the_api(seen: list[str]) -> None:
    calls = AIMessage(
        "",
        tool_calls=[
            {"name": "web_search", "args": {"query": "internal a"}, "id": "a"},
            {"name": "lookup", "args": {"query": "refunds"}, "id": "b"},
            {"name": "web_search", "args": {"query": "internal c"}, "id": "c"},
        ],
    )
    with hitl_client(scripted(calls, "Done."), seen) as c:
        body = c.post("/chat", json={"message": "go"}).json()
        assert len(body["pending"]["action_requests"]) == 2

        decisions = [{"type": "approve"}, {"type": "reject"}]
        resumed = c.post(f"/threads/{body['thread_id']}/resume", json={"decisions": decisions})

    assert resumed.json()["reply"] == "Done."
    assert seen == ["internal a"]


def test_other_users_cannot_see_or_decide(client: TestClient) -> None:
    thread_id = start(client)

    assert client.get(f"/threads/{thread_id}/messages", headers=BOB).status_code == 404
    resp = client.post(
        f"/threads/{thread_id}/resume",
        json={"decisions": [{"type": "approve"}]},
        headers=BOB,
    )
    assert resp.status_code == 404


def test_decisions_for_a_thread_that_is_not_waiting(seen: list[str]) -> None:
    with hitl_client(scripted("Hi!"), seen) as c:
        thread_id = c.post("/chat", json={"message": "hi"}).json()["thread_id"]
        resp = c.post(f"/threads/{thread_id}/resume", json={"decisions": [{"type": "approve"}]})

    assert resp.status_code == 409
