from typing import Any

from fastapi import APIRouter
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from sse_starlette.sse import EventSourceResponse

from chatbot.api.dependencies import Callbacks, Graph
from chatbot.api.schemas import ChatRequest, ChatResponse
from chatbot.api.streaming import stream_run
from chatbot.config import get_settings

router = APIRouter(prefix="/chat", tags=["chat"])


def run_config(body: ChatRequest, callbacks: list[BaseCallbackHandler]) -> RunnableConfig:
    configurable: dict[str, Any] = {}
    if body.thread_id:
        configurable["thread_id"] = body.thread_id
    return {
        "configurable": configurable,
        "callbacks": callbacks,
        "recursion_limit": get_settings().max_agent_steps,
    }


def collect_sources(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    return [
        source
        for m in messages
        if isinstance(m, ToolMessage) and m.artifact
        for source in m.artifact
    ]


@router.post("")
async def chat(body: ChatRequest, graph: Graph, callbacks: Callbacks) -> ChatResponse:
    result = await graph.ainvoke(
        {"messages": [HumanMessage(body.message)]}, config=run_config(body, callbacks)
    )
    return ChatResponse(
        reply=result["messages"][-1].text,
        thread_id=body.thread_id,
        sources=collect_sources(result["messages"]),
    )


@router.post("/stream")
async def chat_stream(body: ChatRequest, graph: Graph, callbacks: Callbacks) -> EventSourceResponse:
    return EventSourceResponse(
        stream_run(graph, {"messages": [HumanMessage(body.message)]}, run_config(body, callbacks))
    )
