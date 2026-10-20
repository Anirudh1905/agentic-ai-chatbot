"""Memory pushes through the queue (Phase 9): the API only enqueues, the worker runs the pushes,
and a push is never dropped: one the worker can't finish stays unacked, and one the API can't
enqueue runs in-process instead. The fake memory model turns each "fact: key=value" line into a
memory (fakes.FactMemoryModel)."""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from fakes import FactMemoryModel, InMemoryThreadStore
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.store.memory import InMemoryStore
from prometheus_client import REGISTRY

from chatbot.jobs import InMemoryJobQueue, MemoryPush
from chatbot.memory.long_term import MemoryWriter, namespace, wait_for_memory_writes
from chatbot.worker import Worker

TURNS: list[AnyMessage] = [HumanMessage("fact: colour=pink", id="h1"), AIMessage("Noted.", id="a1")]


def memories(store: InMemoryStore, user: str = "alice") -> list[str]:
    return sorted(i.value["content"] for i in store.search(namespace(user), limit=100))


class BrokenQueue(InMemoryJobQueue):
    async def enqueue(self, push: MemoryPush) -> None:
        raise ConnectionError("ElasticMQ is down")


class FailingModel(FactMemoryModel):
    def _extract(self, messages: Any) -> Any:
        raise TimeoutError("the gateway stalled")


async def no_messages(thread_id: str) -> Sequence[AnyMessage]:
    return []


async def test_a_summary_is_enqueued_not_written_by_the_api() -> None:
    queue, store = InMemoryJobQueue(), InMemoryStore()
    writer = MemoryWriter(FactMemoryModel(), queue=queue)

    writer.enqueue_summary(store, "alice", "t1", TURNS)
    await wait_for_memory_writes()

    assert memories(store) == []  # the API wrote nothing
    (job,) = list(queue.visible)
    assert job[0].kind == "summary" and job[0].group_id == "alice"
    assert [m.id for m in job[0].message_list()] == ["h1", "a1"]


async def test_the_worker_runs_the_push_and_acks_it() -> None:
    queue, store = InMemoryJobQueue(), InMemoryStore()
    api = MemoryWriter(FactMemoryModel(), queue=queue)
    api.enqueue_summary(store, "alice", "t1", TURNS)
    api.remember(store, "bob", "fact: city=Pune", None)
    await wait_for_memory_writes()

    worker = Worker(queue, MemoryWriter(FactMemoryModel()), store, no_messages)
    assert await worker.run_once(wait_seconds=0) == 2

    assert memories(store) == ["fact: colour=pink"]
    assert memories(store, "bob") == ["fact: city=Pune"]
    queue.expire_in_flight()
    assert await queue.receive() == []  # both acked


async def test_a_push_that_fails_stays_on_the_queue() -> None:
    queue, store = InMemoryJobQueue(), InMemoryStore()
    await queue.enqueue(MemoryPush.summary("alice", "t1", TURNS))
    before = (
        REGISTRY.get_sample_value(
            "chatbot_memory_pushes_total", {"kind": "summary", "outcome": "failed"}
        )
        or 0.0
    )

    worker = Worker(queue, MemoryWriter(FailingModel()), store, no_messages)
    await worker.run_once(wait_seconds=0)

    queue.expire_in_flight()
    (again,) = await queue.receive()
    assert again.receives == 2  # not acked: SQS delivers it again, then dead-letters it
    assert (
        REGISTRY.get_sample_value(
            "chatbot_memory_pushes_total", {"kind": "summary", "outcome": "failed"}
        )
        == before + 1
    )


async def test_a_push_the_api_cant_enqueue_runs_in_process() -> None:
    store = InMemoryStore()
    before = REGISTRY.get_sample_value("chatbot_queue_fallbacks_total") or 0.0
    writer = MemoryWriter(FactMemoryModel(), queue=BrokenQueue())

    writer.enqueue_summary(store, "alice", "t1", TURNS)
    await wait_for_memory_writes()

    assert memories(store) == ["fact: colour=pink"]  # never dropped
    assert REGISTRY.get_sample_value("chatbot_queue_fallbacks_total") == before + 1


async def test_the_idle_scan_enqueues_and_the_worker_reads_the_thread() -> None:
    queue, store, threads = InMemoryJobQueue(), InMemoryStore(), InMemoryThreadStore()
    thread = await threads.create(uuid.uuid4(), "alice", None)

    async def messages_of(thread_id: str) -> Sequence[AnyMessage]:
        return TURNS if thread_id == str(thread.id) else []

    scan = MemoryWriter(FactMemoryModel(), queue=queue)
    assert await scan.push_idle(store, threads, messages_of, timedelta(0)) == 1
    assert memories(store) == []
    (job,) = list(queue.visible)
    assert (job[0].kind, job[0].thread_id) == ("idle", str(thread.id))

    worker = Worker(queue, MemoryWriter(FactMemoryModel()), store, messages_of)
    await worker.run_once(wait_seconds=0)
    assert memories(store) == ["fact: colour=pink"]


async def test_without_a_queue_pushes_run_in_process_as_before() -> None:
    store = InMemoryStore()
    MemoryWriter(FactMemoryModel()).enqueue_summary(store, "alice", "t1", TURNS)
    await wait_for_memory_writes()
    assert memories(store) == ["fact: colour=pink"]
