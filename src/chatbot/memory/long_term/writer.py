"""MemoryWriter: pushes a thread's turns into long-term memory, in the background, on four
triggers (see the package docstring), one push per user at a time."""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore, Item
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from chatbot.config import RunContext
from chatbot.memory.long_term.facts import Changes, Fact, Facts, apply_changes, rejected
from chatbot.memory.long_term.store import cursor_namespace, namespace
from chatbot.memory.short_term import summary_text
from chatbot.memory.threads import Thread, ThreadStore
from chatbot.prompts import load_prompt

logger = logging.getLogger(__name__)

# Existing memories the model sees when deciding what a new fact changes.
CANDIDATES = 5
# The assistant's replies are only context for the user's words; cut them to save tokens.
REPLY_CHARS = 500
# Idle threads are looked at back to this far; older ones were pushed long ago.
TTL_LOOKBACK = timedelta(days=7)
# Tells the memory model the user asked for this message to be remembered.
REMEMBER_THIS = (
    "The user asked to remember the exchange below. Extract facts from both their question and "
    "the assistant's answer: what they said about themselves, and the specific answer they'll "
    "want later (a product, size, rating, date or price; at most two, never general tips), "
    'written as facts about the user ("User was recommended …"). The other rules still apply.'
)


def transcript(messages: Sequence[AnyMessage]) -> str:
    """The user's and the assistant's words, labelled, for the memory model. Tool calls and
    results are left out (third-party data), and so is a running summary (its turns were
    pushed when it was written)."""
    lines = []
    for m in messages:
        if isinstance(m, HumanMessage) and summary_text(m) is None:
            lines.append(f"User: {m.text}")
        elif isinstance(m, AIMessage) and m.text:
            reply = m.text if len(m.text) <= REPLY_CHARS else m.text[:REPLY_CHARS] + " …"
            lines.append(f"Assistant: {reply}")
    return "\n\n".join(lines)


def after_cursor(messages: Sequence[AnyMessage], cursor: Item | None) -> list[AnyMessage]:
    """The messages after the last one pushed. If that one isn't among them (it was summarized
    away), they're all new."""
    last = cursor.value.get("message_id") if cursor else None
    ids = [m.id for m in messages]
    return list(messages[ids.index(last) + 1 :]) if last in ids else list(messages)


class MemoryWriter:
    """Pushes threads' turns into long-term memories, in the background."""

    def __init__(self, model: BaseChatModel, concurrency: int = 2) -> None:
        self.extract = model.with_structured_output(Facts)
        self.decide = model.with_structured_output(Changes)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Each push holds a pooled connection for its lock while it waits on the model, so an
        # unbounded number of them would take every connection, from chat requests too (12
        # users at once on a 10-connection pool: 10 failed with PoolTimeout).
        self._slots = asyncio.Semaphore(concurrency)
        self._loop: asyncio.Task[None] | None = None

    def schedule(self, runtime: Runtime[RunContext], messages: Sequence[AnyMessage]) -> None:
        """Push the turns a summary is folding away (the thread passed CONTEXT_MAX_TOKENS), in
        the background; returns immediately."""
        thread_id = get_config().get("configurable", {}).get("thread_id")
        if runtime.store is None or runtime.context is None or not thread_id:
            return
        self._background(self.push(runtime.store, runtime.context.user_id, thread_id, messages))

    def remember(
        self, store: BaseStore, user_id: str, question: str | None, reply: str | None = None
    ) -> None:
        """Push what the user asked to remember, in the background: their question and the
        assistant's reply together (the "Remember this" button), or just their words (the
        `remember` tool)."""
        lines = [f"User: {question}"] if question else []
        lines += [f"Assistant: {reply}"] if reply else []
        conversation = f"{REMEMBER_THIS}\n\n" + "\n\n".join(lines)
        self._background(self._locked_write(store, user_id, conversation, "a remembered exchange"))

    def start(
        self,
        store: BaseStore,
        threads: ThreadStore,
        messages_of: Callable[[str], Awaitable[Sequence[AnyMessage]]],
        ttl: timedelta,
        every: timedelta,
    ) -> None:
        """Every `every`, push threads idle for `ttl` (`messages_of` reads a thread's
        messages)."""

        async def loop() -> None:
            while True:
                try:
                    await self.push_idle(store, threads, messages_of, ttl)
                except Exception:
                    logger.exception("pushing idle threads to long-term memory failed")
                await asyncio.sleep(every.total_seconds())

        self._loop = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
            self._loop = None
        await wait_for_memory_writes()

    async def push_idle(
        self,
        store: BaseStore,
        threads: ThreadStore,
        messages_of: Callable[[str], Awaitable[Sequence[AnyMessage]]],
        ttl: timedelta,
    ) -> int:
        """Push every thread idle for `ttl` that has changed since it was last pushed. Returns
        how many threads that was. Users are pushed side by side (up to the write slots), so
        one slow push doesn't hold up everyone else's; each user's threads go in order."""
        now = datetime.now(UTC)
        due: dict[str, list[Thread]] = defaultdict(list)
        for thread in await threads.idle_between(now - TTL_LOOKBACK, now - ttl):
            cursor = await store.aget(cursor_namespace(thread.user_id), str(thread.id))
            if cursor and cursor.value.get("thread_updated_at") == thread.updated_at.isoformat():
                continue  # nothing new since the last push
            due[thread.user_id].append(thread)

        async def push_user(user_threads: list[Thread]) -> None:
            for thread in user_threads:
                await self.push(
                    store,
                    thread.user_id,
                    str(thread.id),
                    await messages_of(str(thread.id)),
                    thread_updated_at=thread.updated_at.isoformat(),
                )

        await asyncio.gather(*(push_user(t) for t in due.values()))
        return sum(len(t) for t in due.values())

    async def push(
        self,
        store: BaseStore,
        user_id: str,
        thread_id: str,
        messages: Sequence[AnyMessage],
        thread_updated_at: str | None = None,
    ) -> None:
        """Push a thread's turns that haven't been pushed yet, and move its cursor past them."""
        async with self._slots, self._user_lock(store, user_id):
            # Read the cursor inside the lock: another process may have just pushed.
            cursor = await store.aget(cursor_namespace(user_id), thread_id)
            new = after_cursor(messages, cursor)
            conversation = transcript(new)
            if conversation:
                if (done := await self._write(store, user_id, conversation)) is None:
                    return  # failed; the cursor stays, so the next trigger retries
                logger.info("pushed %d messages to long-term memory: %s", len(new), done)
            last = new[-1].id if new else (cursor.value.get("message_id") if cursor else None)
            await store.aput(
                cursor_namespace(user_id),
                thread_id,
                {"message_id": last, "thread_updated_at": thread_updated_at},
                index=False,
            )

    async def _locked_write(
        self, store: BaseStore, user_id: str, conversation: str, what: str
    ) -> None:
        async with self._slots, self._user_lock(store, user_id):
            if (done := await self._write(store, user_id, conversation)) is not None:
                logger.info("pushed %s to long-term memory: %s", what, done)

    def _background(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    async def _write(self, store: BaseStore, user_id: str, conversation: str) -> str | None:
        """Extract facts from `conversation` and apply them. None if extraction failed (the
        next trigger retries it)."""
        today = datetime.now(UTC).date()
        try:
            facts = cast(
                Facts,
                await self.extract.ainvoke(
                    [
                        SystemMessage(load_prompt("memory_extract").replace("{today}", str(today))),
                        HumanMessage(f"<conversation>\n{conversation}\n</conversation>"),
                    ]
                ),
            )
        except Exception as e:
            if getattr(e, "code", None) == "content_filter":
                # The gateway's content filter rejected these turns. The same turns get the same
                # answer, so retrying would fail on every TTL check for days: give them up.
                logger.warning("long-term memory: the content filter rejected these turns")
                return "skipped (content filter)"
            logger.exception("updating long-term memory failed")
            return None
        done: Counter[str] = Counter()
        for fact in facts.memories:
            if reason := rejected(fact, today):
                done[f"dropped ({reason})"] += 1
                continue
            try:
                done += await self._apply(store, user_id, fact)
            except Exception:
                # One fact failing (a content filter, a timeout) shouldn't lose the rest.
                logger.exception("updating a long-term memory failed")
                done["failed"] += 1
        return describe(done)

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


# Pushes started by `schedule()` and `remember()`, kept so they aren't garbage-collected.
_pending: set[asyncio.Task[None]] = set()


async def wait_for_memory_writes() -> None:
    """Wait for pushes started by `schedule()` and `remember()` (tests, clean shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending))


def describe(done: Counter[str]) -> str:
    return ", ".join(f"{n} {action}" for action, n in sorted(done.items())) or "no changes"
