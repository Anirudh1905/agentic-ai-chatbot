"""The per-thread run lease (Phase 9, api/leases.py): one run at a time per thread."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
import pytest
from apiclient import ALICE, BOB, client_for, scripted
from fakes import InMemoryThreadStore, ScriptedChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from prometheus_client import REGISTRY

from chatbot.agent.builder import build_graph
from chatbot.api.dependencies import get_threads
from chatbot.api.leases import BUSY, ThreadLease, take_lease
from chatbot.api.main import app
from chatbot.api.runs import thread_config
from chatbot.api.streaming import stream_run
from chatbot.config import RunContext, Settings


def threads() -> InMemoryThreadStore:
    store: InMemoryThreadStore = app.dependency_overrides[get_threads]()
    return store


def busy_count() -> float:
    return REGISTRY.get_sample_value("chatbot_limited_total", {"limit": "thread_busy"}) or 0.0


def hold(thread_id: str, seconds: float = 60) -> None:
    """A lease held by some other run (on another pod, say)."""
    threads().leases[uuid.UUID(thread_id)] = (
        uuid.uuid4(),
        datetime.now(UTC) + timedelta(seconds=seconds),
    )


class SlowModel(ScriptedChatModel):
    """Answers after a pause, so a second request arrives while the first is running."""

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        await asyncio.sleep(0.5)
        return self._generate(messages)


def test_every_run_route_answers_409_while_the_thread_is_busy() -> None:
    with client_for(scripted("hi")) as c:
        thread_id = c.post("/threads").json()["id"]
        hold(thread_id)
        before = busy_count()
        responses = [
            c.post("/chat", json={"message": "hello", "thread_id": thread_id}),
            c.post("/chat/stream", json={"message": "hello", "thread_id": thread_id}),
            c.post(f"/threads/{thread_id}/resume"),
            c.post(f"/threads/{thread_id}/resume/stream"),
            c.delete(f"/threads/{thread_id}"),
        ]
        listed = c.get("/threads").json()

    for resp in responses:
        assert resp.status_code == 409, resp.request.url
        assert resp.json()["detail"] == BUSY
        assert resp.headers["retry-after"] == "5"
    assert busy_count() - before == 5
    assert [t["id"] for t in listed] == [thread_id]  # not deleted


def test_someone_elses_busy_thread_is_still_not_found() -> None:
    with client_for(scripted("hi")) as c:
        thread_id = c.post("/threads").json()["id"]
        hold(thread_id)
        resp = c.post("/chat", json={"message": "hello", "thread_id": thread_id}, headers=BOB)

    assert resp.status_code == 404


def test_the_lease_is_released_when_a_run_ends() -> None:
    with client_for(scripted("one", "two", "three")) as c:
        first = c.post("/chat", json={"message": "hello"}).json()["thread_id"]
        c.post("/chat", json={"message": "again", "thread_id": first})
        c.post("/chat/stream", json={"message": "and again", "thread_id": first})
        leases = dict(threads().leases)

    assert leases == {}


def test_the_lease_is_released_when_a_run_fails() -> None:
    # One reply only: the second run's model call raises.
    with client_for(scripted("only one reply")) as c:
        thread_id = c.post("/chat", json={"message": "first"}).json()["thread_id"]
        streamed = c.post("/chat/stream", json={"message": "second", "thread_id": thread_id})
        with pytest.raises(IndexError):
            c.post("/chat", json={"message": "third", "thread_id": thread_id})
        leases = dict(threads().leases)

    assert "event: error" in streamed.text
    assert leases == {}


def test_an_expired_lease_is_taken_over() -> None:
    # Its run's pod died: nobody renewed it.
    with client_for(scripted("hi")) as c:
        thread_id = c.post("/threads").json()["id"]
        hold(thread_id, seconds=-1)
        resp = c.post("/chat", json={"message": "hello", "thread_id": thread_id})

    assert resp.status_code == 200


def test_two_messages_at_once_run_the_model_once() -> None:
    # The Phase 3 bug: both read the same checkpoint and the later write won.
    llm = SlowModel(responses=[AIMessage("first"), AIMessage("second")])
    with client_for(llm) as c:
        thread_id = c.post("/threads").json()["id"]

        async def both() -> list[httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", headers=ALICE
            ) as client:
                body = {"message": "hello", "thread_id": thread_id}
                return list(
                    await asyncio.gather(
                        client.post("/chat", json=body), client.post("/chat", json=body)
                    )
                )

        responses = asyncio.run(both())

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(llm.calls) == 1


async def test_a_long_run_keeps_its_lease_by_renewing_it() -> None:
    store = InMemoryThreadStore()
    thread_id = uuid.uuid4()
    await store.create(thread_id, "alice", None)
    lease = ThreadLease(store, thread_id, seconds=0.3)
    assert await store.lease(thread_id, "alice", lease.run_id, lease.seconds)
    lease.start_renewing()

    await asyncio.sleep(0.8)  # well past the 0.3 s it started with
    taken = await store.lease(thread_id, "alice", uuid.uuid4(), 60)
    await lease.release()

    assert not taken
    assert store.leases == {}


async def test_a_failed_renewal_is_counted_and_the_run_carries_on() -> None:
    class Unreachable(InMemoryThreadStore):
        async def renew(self, thread_id: uuid.UUID, run_id: uuid.UUID, seconds: float) -> bool:
            raise OSError("database unreachable")

    store = Unreachable()
    thread_id = uuid.uuid4()
    await store.create(thread_id, "alice", None)
    before = REGISTRY.get_sample_value("chatbot_thread_lease_renewal_failures_total") or 0.0
    lease = await take_lease(store, thread_id, "alice")
    lease.seconds = 0.15  # renew every 0.05 s from the next round on

    await asyncio.sleep(0.3)
    renewing = lease._renewing
    failures = REGISTRY.get_sample_value("chatbot_thread_lease_renewal_failures_total")
    await lease.release()

    assert renewing is not None and not renewing.done()  # still trying
    assert failures is not None and failures > before
    assert store.leases == {}


async def test_a_stream_the_client_left_releases_its_lease() -> None:
    class OverTheNetwork(InMemoryThreadStore):
        async def release(self, thread_id: uuid.UUID, run_id: uuid.UUID) -> None:
            await asyncio.sleep(0.01)  # a real round trip, where a cancellation can land
            await super().release(thread_id, run_id)

    store = OverTheNetwork()
    thread_id = uuid.uuid4()
    await store.create(thread_id, "alice", None)
    graph = build_graph(
        Settings(),
        tools=[],
        llm=scripted("a reply long enough to stream in many tokens"),
        checkpointer=InMemorySaver(),
    )
    context = RunContext("alice", None)
    lease = await take_lease(store, thread_id, "alice")
    events = stream_run(
        graph,
        {"messages": [HumanMessage("hi")]},
        thread_config(thread_id),
        context,
        thread_id,
        lease,
    )

    # sse-starlette cancels the stream's scope when the client goes.
    with anyio.CancelScope() as scope:
        async for event in events:
            if event["event"] == "token":
                scope.cancel()

    assert scope.cancelled_caught
    assert store.leases == {}
