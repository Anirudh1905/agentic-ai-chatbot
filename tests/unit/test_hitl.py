from typing import Any

import pytest
from fakes import ScriptedChatModel, make_lookup_tool, tool_call
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from chatbot.config import Settings
from chatbot.graph.builder import build_graph
from chatbot.hitl.policy import interrupt_on, sensitive_reason

CONFIG: RunnableConfig = {"configurable": {"thread_id": "t"}}


@pytest.mark.parametrize(
    ("query", "flagged"),
    [
        ("REI return policy", None),
        ("Project Falcon launch date", '"Project Falcon"'),
        ("acme INTERNAL roadmap", '"internal"'),
        ("international shipping rates", None),
        ("jane.doe@example.com order status", "email address"),
        ("where is order AC-55821", "order number"),
        ("call +1 (303) 555-0142 about returns", "phone number"),
        ("best tents of 2026", None),
    ],
)
def test_sensitive_reason(query: str, flagged: str | None) -> None:
    reason = sensitive_reason(query, Settings().hitl_sensitive_terms)

    if flagged is None:
        assert reason is None
    else:
        assert reason is not None and flagged in reason


def web_search_tool(seen: list[str]) -> BaseTool:
    @tool
    async def web_search(query: str) -> str:
        """Search the web."""
        seen.append(query)
        return f"web results for {query}"

    return web_search


@tool
async def send_email(to: str) -> str:
    """Send an email."""
    return "sent"


def test_interrupt_on_config() -> None:
    kb = make_lookup_tool().model_copy(update={"name": "search_knowledge_base"})
    config = interrupt_on([kb, web_search_tool([]), send_email], ["internal"])

    assert config["search_knowledge_base"] is False
    web, email = config["web_search"], config["send_email"]
    assert isinstance(web, dict) and "when" in web
    # Tools without a rule always ask (the middleware would auto-approve them otherwise).
    assert isinstance(email, dict) and "when" not in email


def hitl_graph(responses: list[AIMessage], seen: list[str]) -> tuple[Any, ScriptedChatModel]:
    llm = ScriptedChatModel(responses=responses)
    tools = [web_search_tool(seen), make_lookup_tool()]
    rules = interrupt_on(tools, ["internal"]) | {"lookup": False}
    graph = build_graph(
        Settings(), tools=tools, llm=llm, checkpointer=InMemorySaver(), interrupt_on=rules
    )
    return graph, llm


def assert_every_tool_call_answered(messages: list[BaseMessage]) -> None:
    """What the model API enforces: each tool call is followed by exactly one result."""
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            results = []
            for later in messages[i + 1 :]:
                if not isinstance(later, ToolMessage):
                    break
                results.append(later.tool_call_id)
            assert sorted(results) == sorted(c["id"] for c in m.tool_calls)


async def start(graph: Any) -> dict[str, Any]:
    result = await graph.ainvoke({"messages": [HumanMessage("search it")]}, CONFIG)
    (pending,) = result["__interrupt__"]
    return dict(pending.value)


async def test_safe_call_runs_without_asking() -> None:
    seen: list[str] = []
    graph, _ = hitl_graph([tool_call("web_search", query="REI news"), AIMessage("News.")], seen)

    result = await graph.ainvoke({"messages": [HumanMessage("REI news?")]}, CONFIG)

    assert "__interrupt__" not in result
    assert seen == ["REI news"]


async def test_sensitive_call_pauses_until_approved() -> None:
    seen: list[str] = []
    graph, llm = hitl_graph(
        [tool_call("web_search", query="internal roadmap"), AIMessage("Found it.")], seen
    )

    request = await start(graph)

    assert request["action_requests"] == [
        {
            "name": "web_search",
            "args": {"query": "internal roadmap"},
            "description": 'The query mentions "internal", which may be internal information.',
        }
    ]
    assert seen == []

    resume = Command(resume={"decisions": [{"type": "approve"}]})
    result = await graph.ainvoke(resume, CONFIG)

    assert seen == ["internal roadmap"]
    assert result["messages"][-1].text == "Found it."
    assert_every_tool_call_answered(llm.calls[-1])


async def test_edit_runs_the_edited_args() -> None:
    seen: list[str] = []
    graph, llm = hitl_graph(
        [tool_call("web_search", query="internal roadmap"), AIMessage("Done.")], seen
    )
    await start(graph)

    edited = {"name": "web_search", "args": {"query": "gear trends"}}
    resume = Command(resume={"decisions": [{"type": "edit", "edited_action": edited}]})
    result = await graph.ainvoke(resume, CONFIG)

    assert seen == ["gear trends"]
    tool_result = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert "human reviewer replaced this tool call" in tool_result.text
    assert "web results for gear trends" in tool_result.text
    assert_every_tool_call_answered(llm.calls[-1])


async def test_reject_tells_the_model_and_skips_the_tool() -> None:
    seen: list[str] = []
    graph, llm = hitl_graph(
        [tool_call("web_search", query="internal roadmap"), AIMessage("Okay, I won't.")], seen
    )
    await start(graph)

    decision = {"type": "reject", "message": "that's confidential"}
    result = await graph.ainvoke(Command(resume={"decisions": [decision]}), CONFIG)

    assert seen == []
    rejection = result["messages"][-2]
    assert isinstance(rejection, ToolMessage) and rejection.status == "error"
    assert "that's confidential" in rejection.text
    assert result["messages"][-1].text == "Okay, I won't."
    assert_every_tool_call_answered(llm.calls[-1])


async def test_parallel_calls_with_mixed_decisions() -> None:
    seen: list[str] = []
    calls = AIMessage(
        "",
        tool_calls=[
            {"name": "web_search", "args": {"query": "internal plans"}, "id": "a"},
            {"name": "lookup", "args": {"query": "refunds"}, "id": "b"},
            {"name": "web_search", "args": {"query": "internal prices"}, "id": "c"},
        ],
    )
    graph, llm = hitl_graph([calls, AIMessage("Mixed answer.")], seen)

    request = await start(graph)
    assert [a["args"]["query"] for a in request["action_requests"]] == [
        "internal plans",
        "internal prices",
    ]

    decisions = [{"type": "approve"}, {"type": "reject"}]
    result = await graph.ainvoke(Command(resume={"decisions": decisions}), CONFIG)

    assert seen == ["internal plans"]
    results = {m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)}
    assert results["a"].text == "web results for internal plans"
    assert results["b"].text == "result for refunds"
    assert "rejected" in results["c"].text
    assert_every_tool_call_answered(llm.calls[-1])
