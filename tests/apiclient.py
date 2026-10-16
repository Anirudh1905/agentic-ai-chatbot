"""A TestClient wired to an in-memory graph, checkpointer and thread store."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fakes import InMemoryThreadStore, ScriptedChatModel
from fastapi.testclient import TestClient
from langchain.agents.middleware import InterruptOnConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore

from chatbot.agent.builder import build_graph
from chatbot.api.dependencies import (
    get_callbacks,
    get_checkpointer,
    get_graph,
    get_store,
    get_threads,
    get_writer,
)
from chatbot.api.main import app
from chatbot.config import Settings
from chatbot.memory.long_term import MemoryWriter

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


@contextmanager
def client_for(
    llm: BaseChatModel,
    tools: list[BaseTool] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    store: BaseStore | None = None,
    summarizer: BaseChatModel | None = None,
    memory_writer: MemoryWriter | None = None,
    **settings: Any,
) -> Iterator[TestClient]:
    checkpointer = InMemorySaver()
    graph = build_graph(
        Settings(**settings),
        tools=tools or [],
        llm=llm,
        summarizer=summarizer,
        checkpointer=checkpointer,
        interrupt_on=interrupt_on,
        store=store,
        memory_writer=memory_writer,
    )
    threads = InMemoryThreadStore()
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    app.dependency_overrides[get_threads] = lambda: threads
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_writer] = lambda: memory_writer
    app.dependency_overrides[get_callbacks] = lambda: []
    try:
        with TestClient(app, headers=ALICE) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def scripted(*replies: AIMessage | str) -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[r if isinstance(r, AIMessage) else AIMessage(r) for r in replies]
    )


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events
