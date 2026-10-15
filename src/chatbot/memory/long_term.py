"""Long-term memory: what the bot knows about a user across their threads.

Short-term memory is the current thread (its messages and running summary, Phase 3).
Long-term memory is a set of small memories per user in LangGraph's store, one fact each, at
`("memories", user_id)`, with a vector index on their content:

- **Written** when a thread passes CONTEXT_MAX_TOKENS. The summarization step hands its new
  summary to `MemoryWriter`, which works in the background so the reply never waits:
  1. The memory model splits the summary into facts, each tagged with a scope (about the
     user, or about something they're dealing with) and a topic.
  2. For each fact, the closest existing memories of that user (any topic) are found by
     vector search, and the model decides what changes: add the fact, update the memories it
     corrects, delete the ones it makes obsolete (one fact can make several obsolete).
  One write per user at a time, across API processes too (a Postgres advisory lock).
- **Read** once, when a new thread starts: `LongTermMemory` searches the user's memories
  with the thread's first message and puts the closest ones in the thread's state, where the
  system prompt picks them up.

Memories are written from thread summaries, which come from the thread the API has already
redacted (Phase 5), so no raw PII reaches the store.
"""

import asyncio
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal, NotRequired, cast

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore, Item, SearchItem
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from chatbot.prompts import load_prompt

logger = logging.getLogger(__name__)

# Existing memories the model sees when deciding what a new fact changes.
CANDIDATES = 5
# Earlier versions kept on each memory, newest last.
HISTORY = 5
# Upper bound when listing all of a user's memories (the API, forgetting everything).
LIST_LIMIT = 1000

# Running memory writes, kept so they aren't garbage-collected mid-flight.
_pending: set[asyncio.Task[None]] = set()


class Fact(BaseModel):
    content: str = Field(description='One sentence in the third person ("User ..."), current state')
    scope: Literal["user", "other"] = Field(
        description='"user": about the user themselves; "other": something they are dealing with'
    )
    topic: Literal["personal", "preferences", "communication", "orders", "plans", "issues"]
    forget: bool = Field(False, description="The user asked to forget this")


class Facts(BaseModel):
    memories: list[Fact] = Field(default_factory=list)


class Change(BaseModel):
    action: Literal["ADD", "UPDATE", "DELETE"]
    memory_id: str | None = Field(None, description="The existing memory, for UPDATE and DELETE")
    content: str | None = Field(None, description="The merged content, for UPDATE")


class Changes(BaseModel):
    changes: list[Change] = Field(default_factory=list)


def namespace(user_id: str) -> tuple[str, ...]:
    return ("memories", user_id)


def current_user() -> str | None:
    """The user of the current run, from its config (set by the API from X-User-Id)."""
    return get_config().get("configurable", {}).get("user_id")


async def list_memories(store: BaseStore, user_id: str) -> list[SearchItem]:
    """All of the user's memories, newest first."""
    items = await store.asearch(namespace(user_id), limit=LIST_LIMIT)
    return sorted(items, key=lambda i: i.updated_at, reverse=True)


async def recall(store: BaseStore, user_id: str, query: str, limit: int) -> str:
    """The user's memories closest to `query`, oldest first and dated, so the model can tell
    which is newer if two disagree. "" if there are none."""
    items = await store.asearch(namespace(user_id), query=query, limit=limit)
    return "\n".join(
        f"- ({i.updated_at:%Y-%m-%d}) {i.value['content']}"
        for i in sorted(items, key=lambda i: i.updated_at)
    )


async def apply_changes(
    store: BaseStore, user_id: str, fact: Fact, existing: list[SearchItem], changes: Changes
) -> Counter[str]:
    """Apply the model's changes for one fact. Returns what was done, by action."""
    by_id = {item.key: item for item in existing}
    done: Counter[str] = Counter()
    for change in changes.changes:
        target = by_id.get(change.memory_id or "")
        if change.action == "ADD" and not fact.forget:
            await store.aput(namespace(user_id), uuid.uuid4().hex, record(fact, fact.content))
        elif change.action == "UPDATE" and target:
            await store.aput(
                namespace(user_id),
                target.key,
                record(fact, change.content or fact.content, previous=target),
            )
        elif change.action == "DELETE" and target:
            await store.adelete(namespace(user_id), target.key)
            del by_id[target.key]
        else:
            done["ignored"] += 1  # an unknown id, or an ADD for a forget request
            continue
        done[change.action.lower()] += 1
    # Deleting what a fact replaces without saving the fact itself would lose it
    # ("Bruno passed away" deleting every Bruno memory).
    if done["delete"] and not (done["add"] or done["update"] or fact.forget):
        await store.aput(namespace(user_id), uuid.uuid4().hex, record(fact, fact.content))
        done["add"] += 1
    return done


def record(fact: Fact, content: str, previous: Item | None = None) -> dict[str, Any]:
    """A memory's value. The store keeps its created and updated times."""
    history = [*previous.value.get("history", []), previous.value["content"]] if previous else []
    return {
        "content": content,
        "scope": fact.scope,
        "topic": fact.topic,
        "history": history[-HISTORY:],
    }


class MemoryWriter:
    """Turns thread summaries into memory changes, in the background."""

    def __init__(self, model: BaseChatModel) -> None:
        self.extract = model.with_structured_output(Facts)
        self.decide = model.with_structured_output(Changes)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def schedule(self, store: BaseStore | None, summary: str) -> None:
        """Start updating the current user's memories from `summary`; returns immediately."""
        user_id = current_user()
        if store is None or not user_id:
            return
        task = asyncio.create_task(self._write(store, user_id, summary))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    async def _write(self, store: BaseStore, user_id: str, summary: str) -> None:
        try:
            facts = cast(
                Facts,
                await self.extract.ainvoke(
                    [
                        SystemMessage(load_prompt("memory_extract")),
                        HumanMessage(f"<conversation_summary>\n{summary}\n</conversation_summary>"),
                    ]
                ),
            )
            if not facts.memories:
                logger.info("nothing to remember from this thread's summary")
                return
            done: Counter[str] = Counter()
            # Each fact is decided against what's stored now, so two writes for the same user
            # mustn't interleave: both would add the same new fact.
            async with self._user_lock(store, user_id):
                for fact in facts.memories:
                    done += await self._apply(store, user_id, fact)
            logger.info(
                "updated long-term memory from %d facts: %s",
                len(facts.memories),
                ", ".join(f"{n} {action}" for action, n in sorted(done.items())) or "no changes",
            )
        except Exception:
            # The thread's summary is still in its own state; the next breach tries again.
            logger.exception("updating long-term memory failed")

    async def _apply(self, store: BaseStore, user_id: str, fact: Fact) -> Counter[str]:
        existing = await store.asearch(namespace(user_id), query=fact.content, limit=CANDIDATES)
        listing = "\n".join(f"[{i.key}] {i.value['content']}" for i in existing)
        new = f"FORGET REQUEST: {fact.content}" if fact.forget else fact.content
        changes = cast(
            Changes,
            await self.decide.ainvoke(
                [
                    SystemMessage(load_prompt("memory_decide")),
                    HumanMessage(
                        f"<new_memory>\n{new}\n</new_memory>\n\n"
                        f"<existing_memories>\n{listing or '(none)'}\n</existing_memories>"
                    ),
                ]
            ),
        )
        return await apply_changes(store, user_id, fact, existing, changes)

    @asynccontextmanager
    async def _user_lock(self, store: BaseStore, user_id: str) -> AsyncGenerator[None]:
        """One write per user at a time: within this process, and with a Postgres store, across
        every process that shares the database (a session-level advisory lock)."""
        async with self._locks[user_id]:
            pool = store.conn if isinstance(store, AsyncPostgresStore) else None
            if not isinstance(pool, AsyncConnectionPool):
                yield
                return
            key = f"memories:{user_id}"
            async with pool.connection() as conn:
                await conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
                try:
                    yield
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


async def wait_for_memory_writes() -> None:
    """Wait for running memory writes (tests, and a clean shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending))


class MemoryState(AgentState[Any]):
    # The memories recalled when this thread started; "" if there were none.
    recalled_memories: NotRequired[str]


class LongTermMemory(AgentMiddleware[MemoryState, Any]):
    """At the start of a new thread, recall what the bot knows about the user that's closest
    to their first message."""

    state_schema = MemoryState

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    async def abefore_agent(
        self, state: MemoryState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        if "recalled_memories" in state or runtime.store is None:
            return None
        user_id = current_user()
        questions = [m for m in state["messages"] if isinstance(m, HumanMessage)]
        if not user_id or len(questions) != 1:
            # Threads from before memory existed: record the check and move on.
            return {"recalled_memories": ""}
        memories = await recall(runtime.store, user_id, questions[0].text, self.limit)
        if memories:
            logger.info("recalled %d long-term memories", memories.count("\n") + 1)
        else:
            logger.info("no long-term memories yet")
        return {"recalled_memories": memories}
