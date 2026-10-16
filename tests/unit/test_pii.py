import logging
from typing import Any

import pytest
from apiclient import client_for, parse_sse, scripted
from fakes import make_lookup_tool, tool_call
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool

from chatbot.agent.approvals import interrupt_on
from chatbot.api import runs
from chatbot.api.dependencies import get_graph
from chatbot.api.main import app
from chatbot.config import Settings
from chatbot.pii import StreamRedactor, detect_phone, redact_pii

EMAIL = "jane@example.com"


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("my email is jane.doe@example.com", "my email is [REDACTED_EMAIL]"),
        ("card 4111 1111 1111 1111 please", "card [REDACTED_CREDIT_CARD] please"),
        ("call +1 (303) 555-0142 today", "call [REDACTED_PHONE] today"),
        ("from 192.168.10.24", "from [REDACTED_IP]"),
        ("order AC-55821 for a tent", "order AC-55821 for a tent"),
        ("refunds take 5 business days", "refunds take 5 business days"),
    ],
)
def test_redact_pii(text: str, redacted: str) -> None:
    assert redact_pii(text)[0] == redacted


def test_redact_pii_reports_types_only() -> None:
    _, found = redact_pii("jane@x.com or 303-555-0142, backup bob@y.org")

    assert sorted(found) == ["email", "email", "phone"]


def test_detect_phone_ignores_short_numbers() -> None:
    assert detect_phone("2 tents for $199.99 in 2026") == []
    assert [m["value"] for m in detect_phone("ring 303-555-0142")] == ["303-555-0142"]


def test_detect_phone_ignores_dates() -> None:
    assert detect_phone("arrived on 2026-10-16, due 16-10-2026, (2026-10-09) noted") == []
    assert [m["value"] for m in detect_phone("call +91 98450 12345")] == ["+91 98450 12345"]


def all_text(calls: list[list[Any]]) -> str:
    return "\n".join(m.text for call in calls for m in call)


async def test_raw_pii_never_reaches_the_model_or_any_checkpoint() -> None:
    llm = scripted(tool_call("lookup", query="orders"), "Thanks, noted.")
    with client_for(llm, [make_lookup_tool()]) as client:
        thread_id = client.post("/chat", json={"message": f"I'm {EMAIL}"}).json()["thread_id"]
        stored = client.get(f"/threads/{thread_id}/messages").json()["messages"][0]["content"]
        graph = app.dependency_overrides[get_graph]()
        config = {"configurable": {"thread_id": thread_id}}
        history = [snap async for snap in graph.aget_state_history(config)]

    assert stored == "I'm [REDACTED_EMAIL]"
    assert EMAIL not in all_text(llm.calls)
    # Every checkpoint, including the input one LangGraph writes before any node runs.
    assert history
    assert all(EMAIL not in m.text for snap in history for m in snap.values.get("messages", []))


def test_summarizer_never_sees_raw_pii() -> None:
    # The scripted model is also the summarizer here, so its calls cover both.
    llm = scripted("a " * 100, "SUMMARY", "b")
    with client_for(llm, context_max_tokens=60) as client:
        thread_id = client.post("/chat", json={"message": "call me on 303-555-0142"}).json()[
            "thread_id"
        ]
        client.post("/chat", json={"message": "and?", "thread_id": thread_id})

    assert len(llm.calls) == 3
    assert "303-555-0142" not in all_text(llm.calls)


def test_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "get_settings", lambda: Settings(pii_enabled=False))
    llm = scripted("ok")
    with client_for(llm) as client:
        client.post("/chat", json={"message": f"I'm {EMAIL}"})

    assert llm.calls[0][-1].text == f"I'm {EMAIL}"


def web_search_tool(seen: list[str]) -> BaseTool:
    @tool
    async def web_search(query: str) -> str:
        """Search the web."""
        seen.append(query)
        return "results"

    return web_search


def test_reviewer_edits_and_reject_reasons_are_redacted() -> None:
    seen: list[str] = []
    tools = [web_search_tool(seen)]
    llm = scripted(
        tool_call("web_search", query="internal a"),
        "Done.",
        tool_call("web_search", call_id="c2", query="internal b"),
        "Okay.",
    )
    with client_for(llm, tools, interrupt_on(tools, ["internal"])) as client:
        thread_id = client.post("/chat", json={"message": "go"}).json()["thread_id"]
        edit = {"type": "edit", "edited_action": {"name": "web_search", "args": {"query": EMAIL}}}
        client.post(f"/threads/{thread_id}/resume", json={"decisions": [edit]})
        client.post("/chat", json={"message": "again", "thread_id": thread_id})
        reject = {"type": "reject", "message": f"don't search {EMAIL}"}
        client.post(f"/threads/{thread_id}/resume", json={"decisions": [reject]})

    assert seen == ["[REDACTED_EMAIL]"]
    assert EMAIL not in all_text(llm.calls)
    assert "don't search [REDACTED_EMAIL]" in all_text(llm.calls)


def test_order_numbers_and_acme_contacts_are_kept() -> None:
    llm = scripted(AIMessage("Write to support@acme-outfitters.example about AC-55821."))
    with client_for(llm) as client:
        body = client.post("/chat", json={"message": "status of AC-55821?"}).json()

    assert llm.calls[0][-1].text == "status of AC-55821?"
    assert body["reply"] == "Write to support@acme-outfitters.example about AC-55821."


def test_output_redactions_are_logged_without_values(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    @tool
    async def lookup(query: str) -> str:
        """Look something up."""
        return "Contact the seller at seller@example.org."

    llm = scripted(tool_call("lookup", query="seller"), "Ask Bob: 303-555-0199.")
    with client_for(llm, [lookup]) as client:
        client.post("/chat", json={"message": "who sells it?"})

    assert "redacted PII from a tool result: email" in caplog.text
    assert "redacted PII from the reply: phone" in caplog.text
    assert "seller@example.org" not in caplog.text and "303-555-0199" not in caplog.text


def test_third_party_pii_in_replies_is_redacted_stored_and_streamed() -> None:
    reply = "Their manager is reachable at bob.smith@othercorp.com or 303-555-0199."
    with client_for(scripted(reply, reply)) as client:
        body = client.post("/chat", json={"message": "who runs it?"}).json()
        events = parse_sse(client.post("/chat/stream", json={"message": "again?"}).text)

    expected = "Their manager is reachable at [REDACTED_EMAIL] or [REDACTED_PHONE]."
    assert body["reply"] == expected
    streamed = "".join(str(d["token"]) for n, d in events if n == "token")
    assert streamed == expected


def test_pii_in_tool_results_is_redacted_before_the_model_reads_it() -> None:
    @tool
    async def lookup(query: str) -> str:
        """Look something up."""
        return "Contact the seller at seller@example.org."

    llm = scripted(tool_call("lookup", query="seller"), "Done.")
    with client_for(llm, [lookup]) as client:
        client.post("/chat", json={"message": "who sells it?"})

    assert "seller@example.org" not in all_text(llm.calls)
    assert "Contact the seller at [REDACTED_EMAIL]." in all_text(llm.calls)


@pytest.mark.parametrize("size", [1, 3, 7, 50])
def test_stream_redactor_catches_pii_split_across_tokens(size: int) -> None:
    text = "intro " * 40 + "reach bob.smith@othercorp.com or 303-555-0199 today. " + "outro " * 40
    tokens = [text[i : i + size] for i in range(0, len(text), size)]
    redactor = StreamRedactor()

    out = [redactor.feed(t) for t in tokens] + [redactor.flush()]

    joined = "".join(out)
    assert joined == redact_pii(text)[0]
    assert "othercorp" not in joined and "555-0199" not in joined
    # Text streams before the end, not all at once.
    assert sum(1 for piece in out if piece) > 1
