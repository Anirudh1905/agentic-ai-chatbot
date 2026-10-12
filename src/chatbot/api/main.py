import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from sse_starlette.sse import EventSourceResponse

from chatbot.api.schemas import ChatRequest, ChatResponse, StreamEvent
from chatbot.config import get_settings
from chatbot.graph.builder import build_graph
from chatbot.graph.state import AgentState
from chatbot.observability import build_callbacks, configure_logging

logger = logging.getLogger(__name__)


@lru_cache
def get_graph() -> CompiledStateGraph[AgentState]:
    return build_graph(get_settings())


@lru_cache
def get_callbacks() -> list[BaseCallbackHandler]:
    return build_callbacks(get_settings())


Graph = Annotated[CompiledStateGraph[AgentState], Depends(get_graph)]
Callbacks = Annotated[list[BaseCallbackHandler], Depends(get_callbacks)]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    configure_logging(get_settings())
    yield


app = FastAPI(title="Agentic AI Chatbot", lifespan=lifespan)


def run_config(body: ChatRequest, callbacks: list[BaseCallbackHandler]) -> RunnableConfig:
    configurable: dict[str, Any] = {}
    if body.thread_id:
        configurable["thread_id"] = body.thread_id
    return {"configurable": configurable, "callbacks": callbacks}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
async def chat(body: ChatRequest, graph: Graph, callbacks: Callbacks) -> ChatResponse:
    result = await graph.ainvoke(
        {"messages": [HumanMessage(body.message)]}, config=run_config(body, callbacks)
    )
    return ChatResponse(reply=result["messages"][-1].text, thread_id=body.thread_id)


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, graph: Graph, callbacks: Callbacks) -> EventSourceResponse:
    config = run_config(body, callbacks)

    async def events() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in graph.astream_events(
                {"messages": [HumanMessage(body.message)]}, config=config, version="v2"
            ):
                if event["event"] != "on_chat_model_stream":
                    continue
                chunk: AIMessageChunk = event["data"]["chunk"]
                # .text skips thinking blocks and returns only visible text.
                if text := chunk.text:
                    yield {"event": StreamEvent.TOKEN, "data": json.dumps({"token": text})}
            yield {"event": StreamEvent.DONE, "data": "{}"}
        except Exception:
            logger.exception("stream failed")
            yield {
                "event": StreamEvent.ERROR,
                "data": json.dumps({"message": "Something went wrong. Please try again."}),
            }

    return EventSourceResponse(events())
