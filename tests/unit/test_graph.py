from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from chatbot.config import Settings
from chatbot.graph.builder import build_graph, build_llm


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
