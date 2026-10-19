"""A paused thread resumes from a fresh process (see conftest.py)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakes import ScriptedChatModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from chatbot.agent.approvals import interrupt_on
from chatbot.agent.builder import build_graph
from chatbot.config import Settings
from chatbot.storage.db import Pool, open_pool

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    pool = await open_pool(Settings(database_url=database_url))
    await AsyncPostgresSaver(pool).setup()
    yield pool
    await pool.close()


def make_graph(pool: Pool, replies: list[AIMessage], seen: list[str]) -> Any:
    @tool
    async def web_search(query: str) -> str:
        """Search the web."""
        seen.append(query)
        return f"results for {query}"

    tools: list[BaseTool] = [web_search]
    return build_graph(
        Settings(),
        tools=tools,
        llm=ScriptedChatModel(responses=replies),
        checkpointer=AsyncPostgresSaver(pool),
        interrupt_on=interrupt_on(tools, ["internal"]),
    )


async def test_pending_approval_survives_a_restart(database_url: str, pool: Pool) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": str(uuid.uuid4())}}
    seen: list[str] = []
    graph = make_graph(pool, [tool_call("web_search", query="internal memo")], seen)
    result = await graph.ainvoke({"messages": [HumanMessage("find the memo")]}, config)
    assert result["__interrupt__"]

    # A new pool, saver and graph, like the API process after a restart.
    fresh_pool = await open_pool(Settings(database_url=database_url))
    try:
        graph = make_graph(fresh_pool, [AIMessage("Here it is.")], seen)
        state = await graph.aget_state(config)
        assert state.interrupts[0].value["action_requests"][0]["args"] == {"query": "internal memo"}

        resume = Command(resume={"decisions": [{"type": "approve"}]})
        result = await graph.ainvoke(resume, config)
    finally:
        await fresh_pool.close()

    assert seen == ["internal memo"]
    assert result["messages"][-1].text == "Here it is."
