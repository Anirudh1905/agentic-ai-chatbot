"""MemoryWriter: pushes a thread's turns into long-term memory, in the background, on four
triggers (see the package docstring), one push per user at a time.

With a queue (SQS_QUEUE_URL, Phase 9) the triggers only enqueue a job (chatbot/jobs) and the
worker runs it (`run`); a job the API can't enqueue runs in-process instead, so a push is never
dropped. Without one, every push runs in the process that triggered it."""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore, Item
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from chatbot.accounts.users import UserStore
from chatbot.config import RunContext
from chatbot.jobs import JobQueue, MemoryPush
from chatbot.memory.long_term.facts import Changes, Fact, Facts, apply_changes, rejected
from chatbot.memory.long_term.store import cursor_namespace, namespace
from chatbot.memory.short_term import summary_text
from chatbot.memory.threads import Thread, ThreadStore
from chatbot.observability.metrics import MEMORY_PUSHES, QUEUE_FALLBACKS
from chatbot.prompts import load_prompt

logger = logging.getLogger(__name__)

# Pauses before retrying an enqueue that failed; after the last, the push runs in-process.
ENQUEUE_RETRY_PAUSES = (0.2, 0.5, 0.0)

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


def structured(
    model: BaseChatModel, schema: type[BaseModel], fallback: BaseChatModel | None
) -> Runnable[Any, Any]:
    runnable = model.with_structured_output(schema)
    return (
        runnable.with_fallbacks([fallback.with_structured_output(schema)]) if fallback else runnable
    )


class MemoryWriter:
    """Pushes threads' turns into long-term memories, in the background."""

    def __init__(
        self,
        model: BaseChatModel,
        concurrency: int = 2,
        queue: JobQueue | None = None,
        fallback: BaseChatModel | None = None,
        users: UserStore | None = None,
    ) -> None:
        """`fallback` (FALLBACK_MODEL) answers when `model` fails (Phase 10). With `users`, a
        push for an account that's gone writes nothing (one queued before DELETE /me)."""
        self.extract = structured(model, Facts, fallback)
        self.decide = structured(model, Changes, fallback)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Each push holds a pooled connection for its lock while it waits on the model, so an
        # unbounded number of them would take every connection, from chat requests too (12
        # users at once on a 10-connection pool: 10 failed with PoolTimeout).
        self._slots = asyncio.Semaphore(concurrency)
        self._loop: asyncio.Task[None] | None = None
        self.queue = queue
        self.users = users

    def schedule(self, runtime: Runtime[RunContext], messages: Sequence[AnyMessage]) -> None:
        """Push the turns a summary is folding away (the thread passed CONTEXT_MAX_TOKENS), in
        the background; returns immediately."""
        thread_id = get_config().get("configurable", {}).get("thread_id")
        if runtime.store is None or runtime.context is None or not thread_id:
            return
        self.enqueue_summary(runtime.store, runtime.context.user_id, thread_id, messages)

    def enqueue_summary(
        self, store: BaseStore, user_id: str, thread_id: str, messages: Sequence[AnyMessage]
    ) -> None:
        """The turns a summary folds away. The job carries them: they leave the thread."""
        push = MemoryPush.summary(user_id, thread_id, messages)
        self._background(self._enqueue_or_run(store, push))

    def remember(
        self, store: BaseStore, user_id: str, question: str | None, reply: str | None = None
    ) -> None:
        """Push what the user asked to remember, in the background: their question and the
        assistant's reply together (the "Remember this" button), or just their words (the
        `remember` tool)."""
        push = MemoryPush.remember(user_id, question, reply)
        self._background(self._enqueue_or_run(store, push))

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

        if self.queue is not None:
            # The worker reads each thread and pushes it; SQS keeps each user's jobs in order.
            for thread in (t for user_threads in due.values() for t in user_threads):
                idle = MemoryPush.idle(
                    thread.user_id, str(thread.id), thread.updated_at.isoformat()
                )
                await self._enqueue_or_run(store, idle, messages_of)
            return sum(len(t) for t in due.values())

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

    async def run(
        self,
        store: BaseStore,
        push: MemoryPush,
        messages_of: Callable[[str], Awaitable[Sequence[AnyMessage]]] | None = None,
    ) -> bool:
        """Run one push: the worker's job, or the fallback when it couldn't be enqueued. False
        if it should be tried again (the worker leaves it on the queue)."""
        try:
            if push.kind == "summary":
                assert push.thread_id is not None
                ok = await self.push(store, push.user_id, push.thread_id, push.message_list())
            elif push.kind == "idle":
                assert push.thread_id is not None and messages_of is not None
                ok = await self.push(
                    store,
                    push.user_id,
                    push.thread_id,
                    await messages_of(push.thread_id),
                    thread_updated_at=push.thread_updated_at,
                )
            else:
                lines = [f"User: {push.question}"] if push.question else []
                lines += [f"Assistant: {push.reply}"] if push.reply else []
                conversation = f"{REMEMBER_THIS}\n\n" + "\n\n".join(lines)
                ok = await self._locked_write(
                    store, push.user_id, conversation, "a remembered exchange"
                )
        except Exception:
            logger.exception("a %s push to long-term memory failed", push.kind)
            ok = False
        MEMORY_PUSHES.labels(push.kind, "ok" if ok else "failed").inc()
        return ok

    async def _enqueue_or_run(
        self,
        store: BaseStore,
        push: MemoryPush,
        messages_of: Callable[[str], Awaitable[Sequence[AnyMessage]]] | None = None,
    ) -> None:
        """Hand the push to the worker; without a queue, or if it can't be reached, run it here."""
        if self.queue is not None:
            for attempt, pause in enumerate(ENQUEUE_RETRY_PAUSES):
                try:
                    await self.queue.enqueue(push)
                    return
                except Exception:
                    if attempt == len(ENQUEUE_RETRY_PAUSES) - 1:
                        logger.warning("couldn't enqueue a %s push; running it here", push.kind)
                        QUEUE_FALLBACKS.inc()
                        break
                    await asyncio.sleep(pause)
        await self.run(store, push, messages_of)

    async def push(
        self,
        store: BaseStore,
        user_id: str,
        thread_id: str,
        messages: Sequence[AnyMessage],
        thread_updated_at: str | None = None,
    ) -> bool:
        """Push a thread's turns that haven't been pushed yet, and move its cursor past them.
        False if it failed (the cursor stays, so the next trigger retries)."""
        async with self._slots, self._user_lock(store, user_id):
            if await self._deleted(user_id):
                return True
            # Read the cursor inside the lock: another process may have just pushed.
            cursor = await store.aget(cursor_namespace(user_id), thread_id)
            new = after_cursor(messages, cursor)
            conversation = transcript(new)
            if conversation:
                if (done := await self._write(store, user_id, conversation)) is None:
                    return False  # the cursor stays, so the next trigger retries
                logger.info("pushed %d messages to long-term memory: %s", len(new), done)
            last = new[-1].id if new else (cursor.value.get("message_id") if cursor else None)
            await store.aput(
                cursor_namespace(user_id),
                thread_id,
                {"message_id": last, "thread_updated_at": thread_updated_at},
                index=False,
            )
            return True

    async def _locked_write(
        self, store: BaseStore, user_id: str, conversation: str, what: str
    ) -> bool:
        async with self._slots, self._user_lock(store, user_id):
            if await self._deleted(user_id):
                return True
            if (done := await self._write(store, user_id, conversation)) is None:
                return False
            logger.info("pushed %s to long-term memory: %s", what, done)
            return True

    async def _deleted(self, user_id: str) -> bool:
        """Whether the user's account is gone. Checked inside their lock, which DELETE /me holds
        until the account is deleted, so a push that waited for it writes nothing back. True
        means done: there's nothing to retry."""
        if self.users is None or await self.users.get(user_id) is not None:
            return False
        logger.info("long-term memory push skipped: user %s was deleted", user_id)
        return True

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

    def user_lock(self, store: BaseStore, user_id: str) -> AbstractAsyncContextManager[None]:
        """Waits for the user's push in progress, and holds off new ones (deleting their data)."""
        return self._user_lock(store, user_id)

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
