"""Long-term memory over the API: memories are written at a token breach (in the background)
and recalled when a new thread starts. The fake summarizer keeps "fact: key=value" lines, and
the fake memory model turns each into a memory, updating the one with the same key."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from apiclient import BOB, client_for, scripted
from fakes import FactKeepingSummarizer, FactMemoryModel
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.store.memory import InMemoryStore

from chatbot.memory.long_term import (
    Change,
    Changes,
    Fact,
    apply_changes,
    namespace,
    wait_for_memory_writes,
)

FILLER = " ".join(["chatter"] * 40)


@contextmanager
def memory_client(replies: int, **settings: Any) -> Iterator[tuple[TestClient, InMemoryStore, Any]]:
    store = InMemoryStore()
    llm = scripted(*[f"reply {i} {FILLER}" for i in range(replies)])
    settings = {"context_max_tokens": 120, **settings}
    with client_for(
        llm,
        store=store,
        summarizer=FactKeepingSummarizer(),
        memory_model=FactMemoryModel(),
        **settings,
    ) as client:
        yield client, store, llm


def chat(client: TestClient, message: str, thread_id: str | None = None, **kw: Any) -> str:
    body = {"message": message, **({"thread_id": thread_id} if thread_id else {})}
    return str(client.post("/chat", json=body, **kw).json()["thread_id"])


def settle(client: TestClient) -> None:
    """Wait for the background memory writes started by the last requests."""
    client.portal.call(wait_for_memory_writes)  # type: ignore[union-attr]


def system_prompt(call: list[BaseMessage]) -> str:
    assert isinstance(call[0], SystemMessage)
    return call[0].text


def long_thread(client: TestClient, *facts: str) -> str:
    """A thread long enough to pass the token limit, so its first turn gets summarized."""
    lines = "\n".join(facts)
    thread = chat(client, f"{lines}\n{FILLER}")
    chat(client, f"more {FILLER}", thread)
    settle(client)
    return thread


def memories(store: InMemoryStore, user: str = "alice") -> list[str]:
    return sorted(i.value["content"] for i in store.search(namespace(user), limit=100))


def test_memories_are_written_at_the_token_breach_and_recalled_in_a_new_thread() -> None:
    with memory_client(replies=3) as (client, store, llm):
        long_thread(client, "fact: colour=pink", "fact: city=Pune")
        assert memories(store) == ["fact: city=Pune", "fact: colour=pink"]

        chat(client, "what colour tent should I buy?")

    prompt = system_prompt(llm.calls[-1])
    assert "<user_memories>" in prompt
    assert ") fact: colour=pink" in prompt and ") fact: city=Pune" in prompt


def test_a_newer_fact_updates_its_memory_and_keeps_the_others() -> None:
    with memory_client(replies=5) as (client, store, llm):
        long_thread(client, "fact: colour=pink", "fact: city=Pune")
        long_thread(client, "fact: colour=red")
        chat(client, "pick a colour for me")

    assert memories(store) == ["fact: city=Pune", "fact: colour=red"]
    [colour] = [i for i in store.search(namespace("alice")) if "colour" in i.value["content"]]
    assert colour.value["history"] == ["fact: colour=pink"]
    prompt = system_prompt(llm.calls[-1])
    assert "fact: colour=red" in prompt and "pink" not in prompt


def test_a_forget_request_deletes_the_memory() -> None:
    with memory_client(replies=4) as (client, store, llm):
        long_thread(client, "fact: colour=pink", "fact: size=M")
        long_thread(client, "fact: size=forget")

    assert memories(store) == ["fact: colour=pink"]


def test_short_threads_are_not_remembered() -> None:
    with memory_client(replies=2) as (client, store, llm):
        chat(client, "fact: colour=pink")
        settle(client)
        chat(client, "anything to remember?")

    assert memories(store) == []
    assert "earlier conversations" not in system_prompt(llm.calls[-1])


def test_memories_are_only_recalled_when_a_thread_starts() -> None:
    with memory_client(replies=4) as (client, store, llm):
        ongoing = chat(client, "hello")
        long_thread(client, "fact: colour=pink")
        chat(client, "still here", ongoing)

    # The ongoing thread started before the memory existed, so it doesn't get it mid-way.
    assert "fact: colour=pink" not in system_prompt(llm.calls[-1])


def test_recall_is_capped() -> None:
    with memory_client(replies=3, memory_recall_limit=2) as (client, store, llm):
        long_thread(client, "fact: a=1", "fact: b=2", "fact: c=3")
        chat(client, "hi")

    assert system_prompt(llm.calls[-1]).count(") fact: ") == 2


def test_memories_are_per_user() -> None:
    with memory_client(replies=3) as (client, store, llm):
        long_thread(client, "fact: colour=pink")
        chat(client, "hi", headers=BOB)

    assert memories(store, "bob") == []
    assert "pink" not in system_prompt(llm.calls[-1])


def test_view_and_forget_memories() -> None:
    with memory_client(replies=3) as (client, store, llm):
        assert client.get("/me/memory").json() == {"memories": []}
        long_thread(client, "fact: colour=pink", "fact: city=Pune")

        seen = client.get("/me/memory").json()["memories"]
        assert sorted(m["content"] for m in seen) == ["fact: city=Pune", "fact: colour=pink"]
        assert all(m["id"] and m["topic"] and m["scope"] and m["updated_at"] for m in seen)
        assert client.get("/me/memory", headers=BOB).json() == {"memories": []}

        pink = next(m["id"] for m in seen if "pink" in m["content"])
        assert client.delete(f"/me/memory/{pink}", headers=BOB).status_code == 404
        assert client.delete(f"/me/memory/{pink}").status_code == 204
        assert client.delete(f"/me/memory/{pink}").status_code == 404
        assert memories(store) == ["fact: city=Pune"]

        assert client.delete("/me/memory").status_code == 204
        chat(client, "do you remember me?")

    assert memories(store) == []
    assert "Pune" not in system_prompt(llm.calls[-1])


def test_memory_can_be_turned_off() -> None:
    with memory_client(replies=3, memory_enabled=False) as (client, store, llm):
        long_thread(client, "fact: colour=pink")
        chat(client, "what colour?")

    assert memories(store) == []
    assert "earlier conversations" not in system_prompt(llm.calls[-1])


def test_a_failed_write_keeps_the_memories_and_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    with memory_client(replies=5) as (client, store, llm):
        long_thread(client, "fact: colour=pink")

        def broken(name: str) -> str:
            raise RuntimeError("prompt store down")

        # Only the memory write fails; summarizing the thread itself still works.
        monkeypatch.setattr("chatbot.memory.long_term.load_prompt", broken)
        long_thread(client, "fact: colour=red")

    # The reply still went out; the memories are as they were.
    assert llm.calls
    assert memories(store) == ["fact: colour=pink"]


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
