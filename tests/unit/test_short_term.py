import pytest
from fakes import (
    Crash,
    FactKeepingSummarizer,
    ScriptedChatModel,
    make_flaky_lookup_tool,
    tool_call,
)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from chatbot.config import Settings
from chatbot.graph.builder import build_graph
from chatbot.memory.short_term import (
    close_dangling_tool_calls,
    render_transcript,
    split_for_summary,
)


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


def test_split_keeps_everything_under_budget() -> None:
    messages = [m for i in range(3) for m in turn(i)]
    assert split_for_summary(messages, max_tokens=10_000, keep_turns=2) == 0


def test_split_keeps_last_n_turns() -> None:
    messages = [m for i in range(10) for m in turn(i)]
    budget = count_tokens_approximately(messages) - 1

    cut = split_for_summary(messages, max_tokens=budget, keep_turns=3)

    assert cut == 14
    assert messages[cut].text.startswith("question 7")


def test_split_drops_more_turns_when_the_tail_is_too_big() -> None:
    messages = [m for i in range(10) for m in turn(i, words=200)]
    one_turn = count_tokens_approximately(turn(0, words=200))

    # Half the budget fits two turns, so only two are kept even though keep_turns is 6.
    cut = split_for_summary(messages, max_tokens=int(one_turn * 4.5), keep_turns=6)

    assert len(messages) - cut == 4


def test_split_never_separates_a_tool_call_from_its_result() -> None:
    messages = [m for i in range(8) for m in tool_turn(i)]

    for budget in range(100, count_tokens_approximately(messages), 97):
        cut = split_for_summary(messages, max_tokens=budget, keep_turns=3)
        assert cut == 0 or isinstance(messages[cut], HumanMessage)


def test_split_keeps_an_oversized_current_turn() -> None:
    messages = [*turn(0), *turn(1, words=5000)]

    assert split_for_summary(messages, max_tokens=100, keep_turns=3) == 2


def test_split_needs_at_least_two_turns() -> None:
    assert split_for_summary(turn(0, words=5000), max_tokens=10, keep_turns=3) == 0


def test_transcript_truncates_tool_results() -> None:
    messages = tool_turn(1)
    messages[2] = ToolMessage("x" * 5000, tool_call_id="c1", name="lookup")

    text = render_transcript(messages)

    assert 'Assistant called lookup({"query": "q1"})' in text
    assert f"Tool result (lookup): {'x' * 1500} […]" in text
    assert text.endswith("Assistant: answer 1")


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
    settings = Settings(context_max_tokens=1500, context_keep_turns=3)
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
    assert len(state.values["messages"]) < 20
    # The first turn is long gone from the messages but survived every re-summarization.
    assert "fact: my locker code is 4417" in state.values["summary"]
    assert "fact: my locker code is 4417" in llm.calls[-1][0].text


async def test_summarizer_failure_keeps_full_history() -> None:
    class BrokenSummarizer(FactKeepingSummarizer):
        def _generate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    settings = Settings(context_max_tokens=50, context_keep_turns=1)
    llm = ScriptedChatModel(responses=[AIMessage("a " * 100), AIMessage("b")])
    graph = build_graph(
        settings, llm=llm, summarizer=BrokenSummarizer(), checkpointer=InMemorySaver()
    )

    await graph.ainvoke({"messages": [HumanMessage("one")]}, config())
    result = await graph.ainvoke({"messages": [HumanMessage("two")]}, config())

    assert len(result["messages"]) == 4
    assert "summary" not in result
