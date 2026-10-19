"""Checkpointing, thread metadata and retention against a real Postgres (see conftest.py)."""

import asyncio
import os
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import ScriptedChatModel, make_lookup_tool, tool_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from chatbot.agent.builder import Agent, build_graph
from chatbot.config import Settings
from chatbot.memory.retention import prune_checkpoints
from chatbot.memory.threads import PostgresThreadStore
from chatbot.storage.db import Pool, open_pool

pytestmark = pytest.mark.integration

TESTS_DIR = Path(__file__).resolve().parents[1]
SOURCE = {"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    pool = await open_pool(Settings(database_url=database_url))
    await AsyncPostgresSaver(pool).setup()
    async with pool.connection() as conn:
        await conn.execute(f"TRUNCATE threads, {', '.join(CHECKPOINT_TABLES)}")
    yield pool
    await pool.close()


def make_graph(
    pool: Pool, replies: Sequence[AIMessage | str], tools: Sequence[BaseTool] = ()
) -> tuple[Agent, ScriptedChatModel]:
    llm = ScriptedChatModel(
        responses=[r if isinstance(r, AIMessage) else AIMessage(r) for r in replies]
    )
    graph = build_graph(Settings(), tools=tools, llm=llm, checkpointer=AsyncPostgresSaver(pool))
    return graph, llm


def config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def row_counts(pool: Pool, thread_id: str) -> dict[str, int]:
    counts = {}
    async with pool.connection() as conn:
        for table in CHECKPOINT_TABLES:
            cur = await conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE thread_id = %s", (thread_id,)
            )
            row = await cur.fetchone()
            assert row is not None
            counts[table] = row["n"]
    return counts


async def test_thread_survives_an_api_restart(database_url: str, pool: Pool) -> None:
    thread = str(uuid.uuid4())
    graph, _ = make_graph(
        pool, [tool_call("lookup", query="refunds"), "Five days."], [make_lookup_tool()]
    )
    await graph.ainvoke({"messages": [HumanMessage("refunds?")]}, config(thread))

    # A fresh pool, saver and graph: nothing carried over in memory.
    fresh_pool = await open_pool(Settings(database_url=database_url))
    try:
        graph, llm = make_graph(fresh_pool, ["You asked about refunds."])
        await graph.ainvoke({"messages": [HumanMessage("what did I ask?")]}, config(thread))
    finally:
        await fresh_pool.close()

    sent = llm.calls[0]
    assert [m.type for m in sent] == ["system", "human", "ai", "tool", "ai", "human"]
    assert isinstance(sent[3], ToolMessage) and sent[3].artifact == [SOURCE]


# Runs a turn in a child process whose tool call kills it with SIGKILL: no cleanup, no
# exception handling, exactly like the API process dying.
KILLED_RUN = textwrap.dedent("""
    import asyncio, os, signal, sys
    from fakes import ScriptedChatModel, tool_call
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from chatbot.config import Settings
    from chatbot.storage.db import open_pool
    from chatbot.agent.builder import build_graph

    @tool
    async def lookup(query: str) -> str:
        "Look something up."
        await asyncio.sleep(0.5)  # a slow tool; the previous step's checkpoint lands meanwhile
        os.kill(os.getpid(), signal.SIGKILL)

    async def main(url: str, thread: str) -> None:
        pool = await open_pool(Settings(database_url=url))
        llm = ScriptedChatModel(responses=[tool_call("lookup", query="refunds")])
        graph = build_graph(Settings(), tools=[lookup], llm=llm,
                            checkpointer=AsyncPostgresSaver(pool))
        await graph.ainvoke({"messages": [HumanMessage("refunds?")]},
                            {"configurable": {"thread_id": thread}})

    asyncio.run(main(sys.argv[1], sys.argv[2]))
""")


async def test_run_killed_during_a_tool_call_resumes(database_url: str, pool: Pool) -> None:
    thread = str(uuid.uuid4())
    proc = await asyncio.create_subprocess_exec(
        *[sys.executable, "-c", KILLED_RUN, database_url, thread],
        env={**os.environ, "PYTHONPATH": str(TESTS_DIR)},
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == -9, stderr.decode()

    graph, llm = make_graph(pool, ["Five days."], [make_lookup_tool()])
    state = await graph.aget_state(config(thread))
    assert state.next == ("tools",)

    result = await graph.ainvoke(None, config(thread))

    assert [m.type for m in result["messages"]] == ["human", "ai", "tool", "ai"]
    assert result["messages"][-1].text == "Five days."
    assert len(llm.calls) == 1


async def test_delete_thread_removes_every_checkpoint_row(pool: Pool) -> None:
    doomed, kept = str(uuid.uuid4()), str(uuid.uuid4())
    graph, _ = make_graph(
        pool,
        [tool_call("lookup", query="a"), "A.", "B."],
        [make_lookup_tool()],
    )
    await graph.ainvoke({"messages": [HumanMessage("a?")]}, config(doomed))
    await graph.ainvoke({"messages": [HumanMessage("b?")]}, config(kept))
    assert all((await row_counts(pool, doomed)).values())

    assert graph.checkpointer is not None
    await graph.checkpointer.adelete_thread(doomed)  # type: ignore[union-attr]

    assert await row_counts(pool, doomed) == dict.fromkeys(CHECKPOINT_TABLES, 0)
    assert (await row_counts(pool, kept))["checkpoints"] > 0


async def test_prune_keeps_only_the_latest_checkpoint(pool: Pool) -> None:
    pruned, untouched = str(uuid.uuid4()), str(uuid.uuid4())
    replies: list[Any] = []
    for i in range(3):
        replies += [tool_call("lookup", call_id=f"c{i}", query=f"q{i}"), f"Answer {i}."]
    graph, _ = make_graph(pool, [*replies, "Other.", "Answer 3."], [make_lookup_tool()])
    for i in range(3):
        await graph.ainvoke({"messages": [HumanMessage(f"q{i}?")]}, config(pruned))
    await graph.ainvoke({"messages": [HumanMessage("other")]}, config(untouched))
    before = await graph.aget_state(config(pruned))
    counts_before = await row_counts(pool, pruned)
    other_before = await row_counts(pool, untouched)

    deleted = await prune_checkpoints(pool, [uuid.UUID(pruned)])

    after = await graph.aget_state(config(pruned))
    counts_after = await row_counts(pool, pruned)
    assert deleted == counts_before["checkpoints"] - 1
    assert counts_after["checkpoints"] == 1
    assert counts_after["checkpoint_blobs"] < counts_before["checkpoint_blobs"]
    assert after.values == before.values
    assert await row_counts(pool, untouched) == other_before
    # And the thread carries on as normal.
    result = await graph.ainvoke({"messages": [HumanMessage("more")]}, config(pruned))
    assert len(result["messages"]) == len(before.values["messages"]) + 2


async def test_thread_store_ownership(pool: Pool) -> None:
    store = PostgresThreadStore(pool)
    thread_id = uuid.uuid4()

    created = await store.claim(thread_id, "alice", "First question")
    assert created is not None and created.user_id == "alice"
    assert await store.claim(thread_id, "bob", "Hijack") is None
    assert await store.get(thread_id, "bob") is None

    again = await store.claim(thread_id, "alice", "Second question")
    assert again is not None
    assert again.title == "First question"
    assert again.updated_at > created.updated_at

    other = await store.create(uuid.uuid4(), "alice", None)
    assert [t.id for t in await store.list_for_user("alice", 10)] == [thread_id, other.id][::-1]
    assert await store.list_for_user("bob", 10) == []
    assert set(await store.ids_for_user("alice")) == {thread_id, other.id}

    future = datetime.now(UTC) + timedelta(seconds=1)
    assert set(await store.idle_since(future)) == {thread_id, other.id}
    assert await store.idle_since(datetime.now(UTC) - timedelta(days=1)) == []

    await store.delete(thread_id)
    assert await store.get(thread_id, "alice") is None
