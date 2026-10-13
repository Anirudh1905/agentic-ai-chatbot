import pytest
from fakes import (
    Crash,
    FactKeepingSummarizer,
    ScriptedChatModel,
    make_flaky_lookup_tool,
    make_lookup_tool,
    tool_call,
)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from chatbot.config import Settings
from chatbot.graph.builder import build_graph
from chatbot.graph.middleware import close_dangling_tool_calls, summary_text


def turn(i: int, words: int = 50) -> list[AnyMessage]:
    filler = " ".join(["word"] * words)
    return [HumanMessage(f"question {i} {filler}"), AIMessage(f"answer {i} {filler}")]


def tool_turn(i: int) -> list[AnyMessage]:
    return [
        HumanMessage(f"question {i}"),
        tool_call("lookup", call_id=f"c{i}", query=f"q{i}"),
        ToolMessage("x " * 400, tool_call_id=f"c{i}", name="lookup"),
        AIMessage(f"answer {i}"),
    ]


def config(thread: str = "t") -> RunnableConfig:
    return {"configurable": {"thread_id": thread}}


async def test_summarizing_never_separates_a_tool_call_from_its_result() -> None:
    turns = 12
    responses: list[AIMessage] = []
    for i in range(turns):
        responses += [tool_call("lookup", call_id=f"c{i}", query=f"q{i}"), AIMessage(f"a{i}")]
    llm = ScriptedChatModel(responses=responses)
    graph = build_graph(
        Settings(context_max_tokens=200),
        tools=[make_lookup_tool()],
        llm=llm,
        summarizer=FactKeepingSummarizer(),
        checkpointer=InMemorySaver(),
    )

    for i in range(turns):
        await graph.ainvoke({"messages": [HumanMessage(f"question {i}")]}, config())

    # Whatever was summarized away, every model call saw each tool call with its result.
    for sent in llm.calls:
        for j, m in enumerate(sent):
            if isinstance(m, ToolMessage):
                earlier = [x for x in sent[:j] if isinstance(x, AIMessage) and x.tool_calls]
                assert earlier and m.tool_call_id in {c["id"] for c in earlier[-1].tool_calls}


def test_close_dangling_tool_calls_inserts_placeholder_after_the_call() -> None:
    messages: list[AnyMessage] = [
        HumanMessage("q"),
        AIMessage(
            "",
            tool_calls=[
                {"name": "a", "args": {}, "id": "1"},
                {"name": "b", "args": {}, "id": "2"},
            ],
        ),
        ToolMessage("done", tool_call_id="1"),
        HumanMessage("next"),
    ]

    fixed = close_dangling_tool_calls(messages)

    assert [type(m).__name__ for m in fixed] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert isinstance(fixed[2], ToolMessage) and fixed[2].tool_call_id == "2"
    assert "interrupted" in fixed[2].text
    assert close_dangling_tool_calls(fixed) == fixed


async def test_thread_remembers_earlier_turns() -> None:
    llm = ScriptedChatModel(responses=[AIMessage("Hi Sam."), AIMessage("Your name is Sam.")])
    graph = build_graph(Settings(), llm=llm, checkpointer=InMemorySaver())

    await graph.ainvoke({"messages": [HumanMessage("I'm Sam")]}, config())
    await graph.ainvoke({"messages": [HumanMessage("What's my name?")]}, config())

    assert [m.text for m in llm.calls[1][1:]] == ["I'm Sam", "Hi Sam.", "What's my name?"]


async def test_threads_are_isolated() -> None:
    llm = ScriptedChatModel(responses=[AIMessage("a"), AIMessage("b")])
    graph = build_graph(Settings(), llm=llm, checkpointer=InMemorySaver())

    await graph.ainvoke({"messages": [HumanMessage("one")]}, config("t1"))
    await graph.ainvoke({"messages": [HumanMessage("two")]}, config("t2"))

    assert [m.text for m in llm.calls[1][1:]] == ["two"]


async def test_crash_mid_tool_call_resumes_from_last_checkpoint() -> None:
    llm = ScriptedChatModel(responses=[tool_call("lookup", query="refunds"), AIMessage("5 days.")])
    graph = build_graph(
        Settings(), tools=[make_flaky_lookup_tool()], llm=llm, checkpointer=InMemorySaver()
    )

    with pytest.raises(Crash):
        await graph.ainvoke({"messages": [HumanMessage("refunds?")]}, config())
    state = await graph.aget_state(config())
    assert state.next == ("tools",)

    result = await graph.ainvoke(None, config())

    # The model isn't called again for the step that already finished.
    assert len(llm.calls) == 2
    assert [m.type for m in result["messages"]] == ["human", "ai", "tool", "ai"]
    assert result["messages"][-1].text == "5 days."


async def test_new_message_after_a_crash_gets_a_placeholder_tool_result() -> None:
    llm = ScriptedChatModel(responses=[tool_call("lookup", query="refunds"), AIMessage("Hi.")])
    graph = build_graph(
        Settings(), tools=[make_flaky_lookup_tool()], llm=llm, checkpointer=InMemorySaver()
    )
    with pytest.raises(Crash):
        await graph.ainvoke({"messages": [HumanMessage("refunds?")]}, config())

    await graph.ainvoke({"messages": [HumanMessage("never mind, hi")]}, config())

    sent = llm.calls[1]
    assert [m.type for m in sent] == ["system", "human", "ai", "tool", "human"]


async def test_long_thread_is_summarized_and_stays_under_budget() -> None:
    settings = Settings(context_max_tokens=1500)
    turns = 200
    llm = ScriptedChatModel(responses=[AIMessage(f"Noted, item {i}.") for i in range(turns)])
    summarizer = FactKeepingSummarizer()
    graph = build_graph(settings, llm=llm, summarizer=summarizer, checkpointer=InMemorySaver())
    filler = " ".join(["chatter"] * 40)

    for i in range(turns):
        text = "fact: my locker code is 4417" if i == 0 else f"turn {i} {filler}"
        await graph.ainvoke({"messages": [HumanMessage(text)]}, config())

    # Every model call fits: the messages it was sent stayed within the budget.
    assert max(count_tokens_approximately(c[1:]) for c in llm.calls) <= 1500
    assert summarizer.calls > 1
    state = await graph.aget_state(config())
    assert count_tokens_approximately(state.values["messages"]) <= 1500
    # The first turn is long gone from the messages but survived every re-summarization.
    summaries = [t for m in state.values["messages"] if (t := summary_text(m))]
    assert len(summaries) == 1 and "fact: my locker code is 4417" in summaries[0]
    assert any("fact: my locker code is 4417" in m.text for m in llm.calls[-1])


async def test_summarizer_failure_keeps_full_history() -> None:
    class BrokenSummarizer(FactKeepingSummarizer):
        def _generate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    settings = Settings(context_max_tokens=50)
    llm = ScriptedChatModel(responses=[AIMessage("a " * 100), AIMessage("b")])
    graph = build_graph(
        settings, llm=llm, summarizer=BrokenSummarizer(), checkpointer=InMemorySaver()
    )

    await graph.ainvoke({"messages": [HumanMessage("one")]}, config())
    result = await graph.ainvoke({"messages": [HumanMessage("two")]}, config())

    assert len(result["messages"]) == 4
    assert not any(summary_text(m) for m in result["messages"])


async def test_summarizing_keeps_the_current_question_and_runs_once_per_turn() -> None:
    # Big tool results, so the history passes the limit in the middle of every turn.
    @tool
    async def big_lookup(query: str) -> str:
        """Look something up."""
        return f"result for {query}: " + "detail " * 300

    turns = 6
    responses: list[AIMessage] = []
    for i in range(turns):
        responses += [tool_call("big_lookup", call_id=f"c{i}", query=f"q{i}"), AIMessage(f"a{i}")]
    llm = ScriptedChatModel(responses=responses)
    summarizer = FactKeepingSummarizer()
    graph = build_graph(
        Settings(context_max_tokens=500),
        tools=[big_lookup],
        llm=llm,
        summarizer=summarizer,
        checkpointer=InMemorySaver(),
    )

    for i in range(turns):
        await graph.ainvoke({"messages": [HumanMessage(f"question {i}")]}, config())
        # Both model calls of this turn, including the one after the tool ran, see the question.
        for sent in llm.calls[-2:]:
            asked = [m.text for m in sent if isinstance(m, HumanMessage) and not summary_text(m)]
            assert asked[-1] == f"question {i}"

    assert 0 < summarizer.calls <= turns
