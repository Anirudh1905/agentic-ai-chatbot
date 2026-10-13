from fakes import ScriptedChatModel, make_lookup_tool, tool_call
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chatbot.agent.builder import build_graph, build_llm
from chatbot.config import Settings


async def test_graph_returns_llm_reply() -> None:
    llm = GenericFakeChatModel(messages=iter([AIMessage("hello there")]))
    graph = build_graph(Settings(), llm=llm)

    result = await graph.ainvoke({"messages": [HumanMessage("hi")]})

    assert [m.type for m in result["messages"]] == ["human", "ai"]
    assert result["messages"][-1].text == "hello there"


async def test_system_prompt_is_prepended() -> None:
    seen: list[list[object]] = []

    class RecordingModel(GenericFakeChatModel):
        async def ainvoke(self, input, *args, **kwargs):  # type: ignore[no-untyped-def, override]
            seen.append(list(input))
            return await super().ainvoke(input, *args, **kwargs)

    graph = build_graph(Settings(), llm=RecordingModel(messages=iter([AIMessage("ok")])))
    await graph.ainvoke({"messages": [HumanMessage("hi")]})

    assert isinstance(seen[0][0], SystemMessage)
    assert isinstance(seen[0][1], HumanMessage)


def test_build_llm_uses_settings() -> None:
    settings = Settings(
        openai_api_key="test",
        openai_base_url="https://gateway.example",
        model="gpt-5.4",
        reasoning_effort="medium",
    )
    llm = build_llm(settings)

    assert llm.model_name == "gpt-5.4"
    assert llm.openai_api_base == "https://gateway.example"
    assert llm.reasoning_effort == "medium"
    assert llm.streaming is True
    assert llm.stream_usage is True


async def test_tool_loop_runs_tool_and_returns_final_answer() -> None:
    llm = ScriptedChatModel(
        responses=[tool_call("lookup", query="refunds"), AIMessage("Refunds take 5 days.")]
    )
    graph = build_graph(Settings(), tools=[make_lookup_tool()], llm=llm)

    result = await graph.ainvoke({"messages": [HumanMessage("refunds?")]})

    assert [m.type for m in result["messages"]] == ["human", "ai", "tool", "ai"]
    tool_message = result["messages"][2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "result for refunds"
    assert result["messages"][-1].text == "Refunds take 5 days."
    # The second LLM call sees the tool result.
    assert isinstance(llm.calls[1][-1], ToolMessage)


async def test_system_prompt_includes_todays_date() -> None:
    from datetime import date

    llm = ScriptedChatModel(responses=[AIMessage("ok")])
    graph = build_graph(Settings(), llm=llm)
    await graph.ainvoke({"messages": [HumanMessage("hi")]})

    assert date.today().isoformat() in llm.calls[0][0].text
