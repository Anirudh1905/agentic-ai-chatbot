"""A TestClient wired to an in-memory graph, checkpointer and thread store."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fakes import InMemoryThreadStore, InMemoryUserStore, ScriptedChatModel
from fastapi.testclient import TestClient
from langchain.agents.middleware import InterruptOnConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore

from chatbot.accounts.limits import UsageLimits
from chatbot.agent.builder import build_graph
from chatbot.api.dependencies import (
    get_callbacks,
    get_checkpointer,
    get_graph,
    get_limits,
    get_store,
    get_threads,
    get_users,
    get_writer,
)
from chatbot.api.main import app
from chatbot.api.security import create_access_token
from chatbot.config import Settings, get_settings
from chatbot.memory.long_term import MemoryWriter

# Tokens are signed with this secret; the API under test verifies them with it.
AUTH = Settings(jwt_secret="test-secret-that-is-long-enough-for-hs256")
# Accounts every test client has, so the tokens below belong to someone.
ACCOUNTS = ("alice", "bob", "carol")


def auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(AUTH, user_id)[0]}"}


ALICE = auth("alice")
BOB = auth("bob")


@contextmanager
def client_for(
    llm: BaseChatModel,
    tools: list[BaseTool] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    store: BaseStore | None = None,
    summarizer: BaseChatModel | None = None,
    memory_writer: MemoryWriter | None = None,
    usage: UsageLimits | None = None,
    **settings: Any,
) -> Iterator[TestClient]:
    """`usage`: rate limits and the token budget (in memory), off unless a test passes them."""
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
        usage=usage,
    )
    threads = InMemoryThreadStore()
    app.dependency_overrides[get_graph] = lambda: graph
    app.dependency_overrides[get_checkpointer] = lambda: checkpointer
    app.dependency_overrides[get_threads] = lambda: threads
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_writer] = lambda: memory_writer
    app.dependency_overrides[get_callbacks] = lambda: []
    app.dependency_overrides[get_settings] = lambda: AUTH  # what /auth/login signs with
    app.dependency_overrides[get_limits] = lambda: usage
    users = InMemoryUserStore()
    for account in ACCOUNTS:
        users.add(account)
    app.dependency_overrides[get_users] = lambda: users
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
