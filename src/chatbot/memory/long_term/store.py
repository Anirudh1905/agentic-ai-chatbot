"""Where long-term memories live in the store: reading them, writing and deleting one, and a
per-user version that changes whenever they do."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from langgraph.store.base import BaseStore, SearchItem

if TYPE_CHECKING:  # facts.py imports this module; Fact is only a type hint here
    from chatbot.memory.long_term.facts import Fact


# Upper bound when listing all of a user's memories (the API, forgetting everything).
LIST_LIMIT = 1000


def namespace(user_id: str) -> tuple[str, ...]:
    return ("memories", user_id)


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
        + (f" (until {i.value['valid_until']})" if i.value.get("valid_until") else "")
        for i in sorted(items, key=lambda i: i.updated_at)
    )


def cursor_namespace(user_id: str) -> tuple[str, ...]:
    return ("memory_cursor", user_id)


def version_namespace(user_id: str) -> tuple[str, ...]:
    return ("memory_version", user_id)


async def version(store: BaseStore, user_id: str) -> str | None:
    """When the user's memories last changed; None if they never have. A key lookup, so a
    thread can tell cheaply whether it needs to recall again."""
    item = await store.aget(version_namespace(user_id), "latest")
    return item.value["at"] if item else None


async def touch(store: BaseStore, user_id: str) -> None:
    """Record that the user's memories just changed."""
    now = datetime.now(UTC).isoformat()
    await store.aput(version_namespace(user_id), "latest", {"at": now}, index=False)


async def delete(store: BaseStore, user_id: str, key: str) -> None:
    await store.adelete(namespace(user_id), key)
    await touch(store, user_id)


async def forget_user(store: BaseStore, user_id: str) -> int:
    """Delete everything kept about the user: their memories, thread cursors and version.
    Returns how many memories there were."""
    forgotten = 0
    for ns in (namespace(user_id), cursor_namespace(user_id), version_namespace(user_id)):
        while items := await store.asearch(ns, limit=LIST_LIMIT):
            for item in items:
                await store.adelete(ns, item.key)
            if ns == namespace(user_id):
                forgotten += len(items)
    return forgotten


async def put(
    store: BaseStore,
    user_id: str,
    key: str,
    fact: "Fact",
    content: str,
) -> None:
    """Store a memory. The store keeps its created and updated times; a temporary fact gets a
    TTL, so the store stops returning it after `valid_until` (and its sweeper deletes it)."""
    value = {
        "content": content,
        "scope": fact.scope,
        "topic": fact.topic,
        "valid_until": fact.valid_until.isoformat() if fact.valid_until else None,
    }
    if not store.supports_ttl:
        await store.aput(namespace(user_id), key, value)
    else:
        ttl = None
        if fact.valid_until:
            end = datetime.combine(fact.valid_until + timedelta(days=1), datetime.min.time(), UTC)
            ttl = max((end - datetime.now(UTC)).total_seconds() / 60, 1)
        await store.aput(namespace(user_id), key, value, ttl=ttl)
    await touch(store, user_id)
