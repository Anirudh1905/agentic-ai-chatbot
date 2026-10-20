"""Memory pushes as queue jobs (chatbot/jobs): what each carries, and the ids SQS orders and
dedupes them by. The in-memory queue stands in for SQS in the other tests."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chatbot.jobs import InMemoryJobQueue, MemoryPush


def summary_push() -> MemoryPush:
    return MemoryPush.summary(
        "alice",
        "t1",
        [
            HumanMessage("fact: colour=pink", id="h1"),
            AIMessage("", id="a1", tool_calls=[{"name": "lookup", "args": {}, "id": "c1"}]),
            ToolMessage("a long search result " * 50, tool_call_id="c1", id="t1-tool"),
            AIMessage("Noted.", id="a2"),
        ],
    )


def test_a_summary_push_round_trips_with_its_message_ids() -> None:
    push = summary_push()
    back = MemoryPush.from_json(push.to_json())

    assert back == push
    assert [m.id for m in back.message_list()] == ["h1", "a1", "t1-tool", "a2"]
    assert back.message_list()[0].text == "fact: colour=pink"


def test_tool_results_are_left_out_of_the_job() -> None:
    # The memory writer never reads them (transcript), and they'd make jobs large.
    tool = summary_push().message_list()[2]
    assert isinstance(tool, ToolMessage) and tool.text == ""


def test_pushes_are_grouped_by_user_and_deduped_by_what_they_push() -> None:
    a, b = summary_push(), summary_push()
    assert a.group_id == "alice"
    assert a.dedupe_id == b.dedupe_id  # the same push enqueued twice runs once

    idle = MemoryPush.idle("alice", "t1", thread_updated_at="2026-10-19T10:00:00+00:00")
    later = MemoryPush.idle("alice", "t1", thread_updated_at="2026-10-19T11:00:00+00:00")
    remember = MemoryPush.remember("alice", "my email is [REDACTED_EMAIL]", "Noted.")
    ids = {a.dedupe_id, idle.dedupe_id, later.dedupe_id, remember.dedupe_id}
    assert len(ids) == 4
    assert all(len(i) <= 128 for i in ids)  # SQS's limit


def test_a_remember_push_carries_the_exchange() -> None:
    push = MemoryPush.from_json(MemoryPush.remember("bob", "I'm vegetarian", None).to_json())
    assert (push.kind, push.user_id, push.question, push.reply) == (
        "remember",
        "bob",
        "I'm vegetarian",
        None,
    )


async def test_the_in_memory_queue_redelivers_until_acked() -> None:
    queue = InMemoryJobQueue()
    await queue.enqueue(summary_push())
    await queue.enqueue(summary_push())  # deduped

    first = await queue.receive()
    assert len(first) == 1 and first[0].receives == 1
    assert await queue.receive() == []  # in flight

    queue.expire_in_flight()  # the visibility timeout ran out: not acked, so it comes back
    again = await queue.receive()
    assert again[0].receives == 2
    await queue.ack(again[0])
    queue.expire_in_flight()
    assert await queue.receive() == []
    assert (await queue.depth()).visible == 0
