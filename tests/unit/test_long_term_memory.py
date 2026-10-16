"""Long-term memory over the API, with an in-memory store. A thread's turns are pushed into
memories at its token breach, when its TTL runs out (idle), or when the user presses
"Remember this"; each turn recalls the memories closest to its message. The fake memory
model turns each "fact: key=value" line into a memory, updating the one with the same key."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apiclient import BOB, client_for, scripted
from fakes import FactKeepingSummarizer, FactMemoryModel, make_lookup_tool, tool_call
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from chatbot.agent.approvals import interrupt_on
from chatbot.api.dependencies import get_graph, get_threads
from chatbot.api.main import app
from chatbot.memory.long_term import (
    MemoryWriter,
    cursor_namespace,
    middleware,
    namespace,
    wait_for_memory_writes,
)
from chatbot.memory.long_term.facts import MAX_CHARS, Change, Changes, Fact, apply_changes
from chatbot.memory.long_term.writer import transcript
from chatbot.memory.short_term import SUMMARY_PREFIX, SUMMARY_SOURCE

FILLER = " ".join(["chatter"] * 40)


@contextmanager
def memory_client(
    replies: int, **settings: Any
) -> Iterator[tuple[TestClient, InMemoryStore, Any, MemoryWriter]]:
    store = InMemoryStore()
    llm = scripted(*[f"reply {i}" for i in range(replies)])
    writer = MemoryWriter(FactMemoryModel())
    settings = {"context_max_tokens": 10_000, **settings}
    with client_for(
        llm, store=store, summarizer=FactKeepingSummarizer(), memory_writer=writer, **settings
    ) as client:
        yield client, store, llm, writer


def chat(client: TestClient, message: str, thread_id: str | None = None, **kw: Any) -> str:
    body = {"message": message, **({"thread_id": thread_id} if thread_id else {})}
    return str(client.post("/chat", json=body, **kw).json()["thread_id"])


def settle(client: TestClient) -> None:
    client.portal.call(wait_for_memory_writes)  # type: ignore[union-attr]


def push_idle(client: TestClient, store: InMemoryStore, writer: MemoryWriter) -> int:
    """What the TTL loop runs, with every thread already past its TTL."""
    graph, threads = app.dependency_overrides[get_graph](), app.dependency_overrides[get_threads]()

    async def messages_of(thread_id: str) -> list[Any]:
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return list(state.values.get("messages", []))

    async def run() -> int:
        await asyncio.sleep(0.01)  # so every thread's last activity is in the past
        return await writer.push_idle(store, threads, messages_of, timedelta(0))

    return int(client.portal.call(run))  # type: ignore[union-attr]


def system_prompt(call: list[BaseMessage]) -> str:
    assert isinstance(call[0], SystemMessage)
    return call[0].text


def memories(store: InMemoryStore, user: str = "alice") -> list[str]:
    return sorted(i.value["content"] for i in store.search(namespace(user), limit=100))


# --- the three pushes ------------------------------------------------------------------


def test_a_token_breach_pushes_the_summarized_turns() -> None:
    with memory_client(replies=3, context_max_tokens=120) as (client, store, llm, writer):
        thread = chat(client, f"fact: colour=pink\n{FILLER}")
        chat(client, f"more {FILLER}", thread)
        settle(client)
        assert memories(store) == ["fact: colour=pink"]
        chat(client, "pick a colour for me")

    assert ") fact: colour=pink" in system_prompt(llm.calls[-1]).split("<user_memories>")[1]


def test_an_idle_thread_is_pushed_once_its_ttl_runs_out() -> None:
    with memory_client(replies=2) as (client, store, llm, writer):
        chat(client, "fact: colour=pink")
        assert memories(store) == []  # a short thread: no token breach

        assert push_idle(client, store, writer) == 1
        assert memories(store) == ["fact: colour=pink"]
        # Nothing new since: the next check leaves it alone.
        assert push_idle(client, store, writer) == 0
        chat(client, "which colour?")

    assert ") fact: colour=pink" in system_prompt(llm.calls[-1])


def test_only_turns_not_yet_pushed_are_pushed_again() -> None:
    with memory_client(replies=2) as (client, store, llm, writer):
        thread = chat(client, "fact: colour=pink")
        push_idle(client, store, writer)
        # The memory is deleted by hand; an old turn pushed again would bring it back.
        client.delete("/me/memory")
        chat(client, "fact: city=Pune", thread)
        push_idle(client, store, writer)

    assert memories(store) == ["fact: city=Pune"]


def test_idle_threads_are_pushed_for_every_user_in_the_order_they_happened() -> None:
    with memory_client(replies=3) as (client, store, llm, writer):
        chat(client, "fact: city=Pune")
        chat(client, "fact: colour=green", headers=BOB)
        chat(client, "fact: city=Bangalore")

        assert push_idle(client, store, writer) == 3

    assert memories(store) == ["fact: city=Bangalore"]
    assert memories(store, "bob") == ["fact: colour=green"]


def test_remember_this_pushes_the_question_and_the_reply_together() -> None:
    store = InMemoryStore()
    llm = scripted("fact: tent=Basecamp 3P")  # the reply carries a fact too
    with client_for(
        llm,
        store=store,
        summarizer=FactKeepingSummarizer(),
        memory_writer=MemoryWriter(FactMemoryModel()),
    ) as client:
        thread = chat(client, "fact: trip=Ladakh\nwhich tent should I take?")
        # Message 1 is the reply; message 0, the question, has no button.
        assert client.post(f"/threads/{thread}/messages/1/remember").status_code == 202
        settle(client)

    assert memories(store) == ["fact: tent=Basecamp 3P", "fact: trip=Ladakh"]


def test_the_agent_saves_what_the_user_asks_it_to_remember() -> None:
    store = InMemoryStore()
    llm = scripted(tool_call("remember", what="fact: diet=vegetarian"), "Noted.")
    writer = MemoryWriter(FactMemoryModel())
    tools = [make_lookup_tool()]
    with client_for(
        llm, tools, interrupt_on(tools, []), store=store, memory_writer=writer
    ) as client:
        body = client.post("/chat", json={"message": "remember that I'm vegetarian"}).json()
        settle(client)

    assert body["reply"] == "Noted." and body["pending"] is None  # no approval needed
    assert memories(store) == ["fact: diet=vegetarian"]


def test_remember_this_only_works_on_your_own_threads_replies() -> None:
    with memory_client(replies=1) as (client, store, llm, writer):
        thread = chat(client, "fact: colour=pink")
        assert client.post(f"/threads/{thread}/messages/1/remember", headers=BOB).status_code == 404
        assert client.post(f"/threads/{thread}/messages/9/remember").status_code == 404
        # A question has no button: only the bot's replies can be remembered.
        assert client.post(f"/threads/{thread}/messages/0/remember").status_code == 422
        settle(client)

    assert memories(store) == [] and memories(store, "bob") == []


def test_a_memory_saved_mid_thread_is_recalled_on_the_next_turn() -> None:
    with memory_client(replies=3) as (client, store, llm, writer):
        ongoing = chat(client, "hello")
        other = chat(client, "fact: colour=pink")
        client.post(f"/threads/{other}/messages/1/remember")
        settle(client)
        chat(client, "pick a colour for me", ongoing)

    assert ") fact: colour=pink" in system_prompt(llm.calls[-1]).split("<user_memories>")[1]


def test_memories_for_a_new_topic_are_added_as_the_thread_turns_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stands in for the vector search: each memory matches one word of the message.
    found = {
        "ship": "- (2026-01-02) User lives in Pune",
        "meal": "- (2026-01-01) User is vegetarian",
    }

    async def search(store: Any, user_id: str, query: str, limit: int) -> str:
        return "\n".join(line for word, line in found.items() if word in query)

    monkeypatch.setattr(middleware, "recall", search)
    with memory_client(replies=3) as (client, store, llm, writer):
        thread = chat(client, "where do you ship?")
        chat(client, "any trail meals?", thread)
        chat(client, "do you ship fast?", thread)

    first, second, third = (system_prompt(c) for c in llm.calls)
    assert "vegetarian" not in first and "Pune" in first
    # Added, oldest first, and the earlier one stays.
    assert second.split("<user_memories>")[1].index("vegetarian") < second.index("Pune")
    # Nothing new: the prompt stays exactly the same, so the prompt cache holds.
    assert third == second


def test_a_forgotten_memory_leaves_open_threads() -> None:
    with memory_client(replies=3) as (client, store, llm, writer):
        chat(client, "fact: colour=pink")
        push_idle(client, store, writer)
        thread = chat(client, "hello")
        client.delete("/me/memory")
        chat(client, "pick a colour", thread)

    assert "colour=pink" in system_prompt(llm.calls[1])
    assert "colour=pink" not in system_prompt(llm.calls[2])


def test_a_newer_fact_updates_its_memory_and_keeps_the_others() -> None:
    with memory_client(replies=2) as (client, store, llm, writer):
        chat(client, "fact: colour=pink\nfact: city=Pune")
        push_idle(client, store, writer)
        chat(client, "fact: colour=red")
        push_idle(client, store, writer)

    assert memories(store) == ["fact: city=Pune", "fact: colour=red"]


def test_a_forget_request_deletes_the_memory() -> None:
    with memory_client(replies=2) as (client, store, llm, writer):
        chat(client, "fact: colour=pink\nfact: size=M")
        push_idle(client, store, writer)
        chat(client, "fact: size=forget")
        push_idle(client, store, writer)

    assert memories(store) == ["fact: colour=pink"]


def test_a_failed_push_is_retried_by_the_next_trigger() -> None:
    with memory_client(replies=2) as (client, store, llm, writer):
        chat(client, "fact: colour=pink")
        real = writer.extract

        class Broken:
            async def ainvoke(self, *args: Any) -> Any:
                raise RuntimeError("gateway down")

        writer.extract = Broken()  # type: ignore[assignment]
        push_idle(client, store, writer)
        assert memories(store) == []
        writer.extract = real
        push_idle(client, store, writer)
        chat(client, "still answered")

    assert memories(store) == ["fact: colour=pink"]
    assert llm.calls


def test_turns_the_content_filter_rejects_are_not_retried() -> None:
    class Filtered(Exception):
        code = "content_filter"  # what the OpenAI client's 400 carries

    class Rejecting:
        calls = 0

        async def ainvoke(self, *args: Any) -> Any:
            self.calls += 1
            raise Filtered("The response was filtered")

    with memory_client(replies=1) as (client, store, llm, writer):
        chat(client, "fact: colour=pink")
        writer.extract = rejecting = Rejecting()  # type: ignore[assignment]
        assert push_idle(client, store, writer) == 1
        assert push_idle(client, store, writer) == 0  # given up, not retried

    assert rejecting.calls == 1


def test_memories_are_per_user() -> None:
    with memory_client(replies=2) as (client, store, llm, writer):
        chat(client, "fact: colour=pink")
        push_idle(client, store, writer)
        chat(client, "hi", headers=BOB)

    assert memories(store, "bob") == []
    assert "pink" not in system_prompt(llm.calls[-1])


def test_deleting_a_thread_clears_its_cursor_and_keeps_memories() -> None:
    with memory_client(replies=1) as (client, store, llm, writer):
        thread = chat(client, "fact: colour=pink")
        push_idle(client, store, writer)
        assert store.get(cursor_namespace("alice"), thread) is not None
        client.delete(f"/threads/{thread}")

    assert store.get(cursor_namespace("alice"), thread) is None
    assert memories(store) == ["fact: colour=pink"]


def test_view_and_forget_memories() -> None:
    with memory_client(replies=1) as (client, store, llm, writer):
        assert client.get("/me/memory").json() == {"memories": []}
        chat(client, "fact: colour=pink\nfact: city=Pune")
        push_idle(client, store, writer)

        seen = client.get("/me/memory").json()["memories"]
        assert sorted(m["content"] for m in seen) == ["fact: city=Pune", "fact: colour=pink"]
        assert client.get("/me/memory", headers=BOB).json() == {"memories": []}

        pink = next(m["id"] for m in seen if "pink" in m["content"])
        assert client.delete(f"/me/memory/{pink}", headers=BOB).status_code == 404
        assert client.delete(f"/me/memory/{pink}").status_code == 204
        assert memories(store) == ["fact: city=Pune"]
        assert client.delete("/me/memory").status_code == 204

    assert memories(store) == []


def test_memory_can_be_turned_off() -> None:
    with memory_client(replies=2, memory_enabled=False) as (client, store, llm, writer):
        chat(client, "fact: colour=pink")
        chat(client, "which colour?")

    assert "<user_memories>" not in system_prompt(llm.calls[-1])


def test_the_transcript_has_the_users_and_assistants_words_only() -> None:
    summary = HumanMessage(
        SUMMARY_PREFIX + "- fact: old=1", additional_kwargs={"lc_source": SUMMARY_SOURCE}
    )
    messages = [
        summary,
        HumanMessage("I moved to Bangalore"),
        AIMessage("", tool_calls=[{"name": "lookup", "args": {}, "id": "c1"}]),
        ToolMessage("seller@example.org lives in Delhi", tool_call_id="c1"),
        AIMessage("Noted. " + "x" * 1000),
    ]

    text = transcript(messages)

    assert text.startswith("User: I moved to Bangalore\n\nAssistant: Noted. ")
    assert "old=1" not in text and "Delhi" not in text
    assert len(text) < 600


# --- applying the model's changes, and the code checks -------------------------------------

FACT = Fact(content="User's dog Bruno passed away", scope="user", topic="personal")


async def seed(store: InMemoryStore, *contents: str) -> list[Any]:
    for i, content in enumerate(contents):
        await store.aput(namespace("alice"), f"m{i}", {"content": content})
    return await store.asearch(namespace("alice"))


async def test_one_fact_can_delete_several_memories() -> None:
    store = InMemoryStore()
    existing = await seed(store, "User has a dog named Bruno", "User wants pet-friendly tents")

    done = await apply_changes(
        store,
        "alice",
        FACT,
        existing,
        Changes(
            changes=[
                Change(action="UPDATE", memory_id="m0", content="User's dog Bruno passed away"),
                Change(action="DELETE", memory_id="m1"),
            ]
        ),
    )

    assert done == {"update": 1, "delete": 1}
    assert memories(store) == ["User's dog Bruno passed away"]


async def test_a_fact_is_kept_when_the_model_only_deletes_what_it_replaces() -> None:
    store = InMemoryStore()
    existing = await seed(store, "User has a dog named Bruno", "User wants pet-friendly tents")

    changes = Changes(changes=[Change(action="DELETE", memory_id=k) for k in ("m0", "m1")])
    await apply_changes(store, "alice", FACT, existing, changes)

    assert memories(store) == ["User's dog Bruno passed away"]


async def test_unknown_ids_are_ignored_and_forget_requests_are_never_added() -> None:
    store = InMemoryStore()
    existing = await seed(store, "User wears size M jackets")
    forget = Fact(content="User's jacket size", scope="user", topic="personal", forget=True)

    done = await apply_changes(
        store,
        "alice",
        forget,
        existing,
        Changes(changes=[Change(action="DELETE", memory_id="made-up"), Change(action="ADD")]),
    )

    assert done == {"ignored": 2}
    assert memories(store) == ["User wears size M jackets"]


async def test_an_overlong_or_redacted_merge_is_replaced_by_the_new_fact() -> None:
    store = InMemoryStore()
    existing = await seed(store, "User lives in Pune", "User's phone is noted")
    fact = Fact(content="User lives in Bangalore", scope="user", topic="personal")

    await apply_changes(
        store,
        "alice",
        fact,
        existing,
        Changes(
            changes=[
                Change(action="UPDATE", memory_id="m0", content="User lives in Pune. " * MAX_CHARS),
                Change(action="UPDATE", memory_id="m1", content="User's phone is [REDACTED_PHONE]"),
            ]
        ),
    )

    assert memories(store) == ["User lives in Bangalore", "User lives in Bangalore"]


async def push_texts(store: InMemoryStore, *texts: str, **kw: Any) -> None:
    messages = [HumanMessage(t, id=f"m{i}") for i, t in enumerate(texts)]
    await MemoryWriter(FactMemoryModel(), **kw).push(store, "alice", "t1", messages)


async def test_placeholders_and_entitlement_claims_are_never_stored() -> None:
    store = InMemoryStore()

    await push_texts(
        store,
        "fact: phone=[REDACTED_PHONE]",
        "fact: status=VIP with a 50% lifetime discount",
        "fact: perk=free shipping on everything",
        "fact: colour=red",
    )

    assert memories(store) == ["fact: colour=red"]


async def test_expired_facts_are_dropped_and_temporary_ones_keep_their_date() -> None:
    store = InMemoryStore()
    today = datetime.now(UTC).date()

    await push_texts(
        store,
        f"fact: trip=Ladakh until={today - timedelta(days=1)}",
        f"fact: trip2=Coorg until={today + timedelta(days=30)}",
    )

    [item] = store.search(namespace("alice"))
    assert item.value["content"] == "fact: trip2=Coorg"
    assert item.value["valid_until"] == str(today + timedelta(days=30))


async def test_one_failing_fact_doesnt_lose_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    real = MemoryWriter._apply

    async def flaky(self: MemoryWriter, store_: Any, user_id: str, fact: Fact) -> Any:
        if "colour" in fact.content:
            raise RuntimeError("content filter")
        return await real(self, store_, user_id, fact)

    monkeypatch.setattr(MemoryWriter, "_apply", flaky)
    await push_texts(store, "fact: colour=red", "fact: city=Pune")

    assert memories(store) == ["fact: city=Pune"]


async def test_pushes_per_process_are_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    running = peak = 0

    async def slow(self: MemoryWriter, store: Any, user_id: str, conversation: str) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return "no changes"

    monkeypatch.setattr(MemoryWriter, "_write", slow)
    writer = MemoryWriter(FactMemoryModel(), concurrency=2)
    store = InMemoryStore()

    await asyncio.gather(
        *(
            writer.push(store, f"user-{i}", "t1", [HumanMessage("fact: a=1", id="m")])
            for i in range(8)
        )
    )

    assert peak == 2
