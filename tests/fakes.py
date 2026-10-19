"""Test doubles shared across test modules."""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool, tool

from chatbot.memory.long_term.facts import Change, Changes, Fact, Facts
from chatbot.memory.threads import Thread
from chatbot.users import DUMMY_HASH, EmailTaken, User, password_hash


class ScriptedChatModel(BaseChatModel):
    """Returns the given AIMessages in order, streaming text word by word and tool calls whole."""

    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._next(messages)
        if message.tool_calls:
            chunk = AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": tc["name"],
                        "args": json.dumps(tc["args"]),
                        "id": tc["id"],
                        "index": i,
                        "type": "tool_call_chunk",
                    }
                    for i, tc in enumerate(message.tool_calls)
                ],
            )
            yield ChatGenerationChunk(message=chunk)
            return
        for piece in re.split(r"(\s+)", message.text):
            if not piece:
                continue
            gen = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=gen)
            yield gen


def tool_call(name: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def make_lookup_tool() -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def lookup(query: str) -> tuple[str, list[dict[str, Any]]]:
        """Look something up."""
        return f"result for {query}", [{"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}]

    return lookup


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so ToolNode doesn't catch it."""


def make_flaky_lookup_tool(failures: int = 1) -> BaseTool:
    """Like make_lookup_tool, but the first `failures` calls crash the run."""
    calls = {"n": 0}

    @tool(response_format="content_and_artifact")
    async def lookup(query: str) -> tuple[str, list[dict[str, Any]]]:
        """Look something up."""
        calls["n"] += 1
        if calls["n"] <= failures:
            raise Crash
        return f"result for {query}", [{"type": "kb", "cite_as": "Doc › Section", "doc_id": "d"}]

    return lookup


class FactKeepingSummarizer(BaseChatModel):
    """Summarizes by keeping every line that contains "fact:", from the old summary and the
    transcript alike. Lets tests check that facts survive repeated summarization. A fact
    written as "fact: key=value" replaces an earlier one with the same key."""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fact-keeping"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        # Stop at "<" and "&": the summary middleware sends messages as XML.
        facts: dict[str, str] = {}
        for fact in re.findall(r"fact: [^\n<&]*", messages[-1].text):
            facts[fact.split("=", 1)[0]] = fact.strip()
        summary = "\n".join(f"- {f}" for f in facts.values())
        return ChatResult(generations=[ChatGeneration(message=AIMessage(summary))])


class FactMemoryModel(BaseChatModel):
    """Stands in for the memory model, with "fact: key=value" lines (see FactKeepingSummarizer).

    Every such line in the user's messages is a memory; "fact: key=forget" asks to forget `key`,
    and "fact: key=value until=YYYY-MM-DD" is temporary. A new memory updates the existing one
    with the same key (or deletes it, for a forget request), and is added otherwise."""

    @property
    def _llm_type(self) -> str:
        return "fact-memory"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("only used through with_structured_output")

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable[Any, Any]:
        return RunnableLambda(self._extract if schema is Facts else self._decide)

    @staticmethod
    def _key(text: str) -> str:
        return text.split("=", 1)[0]

    def _extract(self, messages: list[BaseMessage]) -> Facts:
        memories = []
        for line in re.findall(r"fact: [^\n<]*", messages[-1].text):
            content, _, until = line.strip().partition(" until=")
            memories.append(
                Fact(
                    content=content,
                    scope="user",
                    topic="preferences",
                    forget=content.endswith("=forget"),
                    valid_until=date.fromisoformat(until) if until else None,
                )
            )
        return Facts(memories=memories)

    def _decide(self, messages: list[BaseMessage]) -> Changes:
        request = messages[-1].text
        new = re.search(r"fact: [^\n<]*", request.split("<existing_memories>")[0])
        assert new
        fact = new.group().strip()
        for memory_id, content in re.findall(r"\[(\w+)\] (fact: [^\n]*)", request):
            if self._key(content) == self._key(fact):
                if "FORGET REQUEST" in request:
                    return Changes(changes=[Change(action="DELETE", memory_id=memory_id)])
                return Changes(changes=[Change(action="UPDATE", memory_id=memory_id, content=fact)])
        return Changes(changes=[Change(action="ADD")])


class InMemoryThreadStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, Thread] = {}

    async def create(self, thread_id: UUID, user_id: str, title: str | None) -> Thread:
        now = datetime.now(UTC)
        self.rows[thread_id] = Thread(thread_id, user_id, title, now, now)
        return self.rows[thread_id]

    async def claim(self, thread_id: UUID, user_id: str, title: str | None) -> Thread | None:
        thread = self.rows.get(thread_id)
        if thread is None:
            return await self.create(thread_id, user_id, title)
        if thread.user_id != user_id:
            return None
        self.rows[thread_id] = replace(
            thread, title=thread.title or title, updated_at=datetime.now(UTC)
        )
        return self.rows[thread_id]

    async def get(self, thread_id: UUID, user_id: str) -> Thread | None:
        thread = self.rows.get(thread_id)
        return thread if thread and thread.user_id == user_id else None

    async def list_for_user(self, user_id: str, limit: int) -> list[Thread]:
        mine = [t for t in self.rows.values() if t.user_id == user_id]
        return sorted(mine, key=lambda t: t.updated_at, reverse=True)[:limit]

    async def delete(self, thread_id: UUID) -> None:
        self.rows.pop(thread_id, None)

    async def ids_for_user(self, user_id: str) -> list[UUID]:
        return [t.id for t in self.rows.values() if t.user_id == user_id]

    async def idle_since(self, cutoff: datetime) -> list[UUID]:
        return [t.id for t in self.rows.values() if t.updated_at < cutoff]

    async def idle_between(self, since: datetime, until: datetime) -> list[Thread]:
        rows = [t for t in self.rows.values() if since <= t.updated_at < until]
        return sorted(rows, key=lambda t: t.updated_at)


class InMemoryUserStore:
    """UserStore with real argon2 hashing, so the login tests check the same verification."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[User, str]] = {}  # by email

    async def create(self, email: str, password: str) -> User:
        if email.lower() in self.rows:
            raise EmailTaken(email)
        user = User(str(uuid4()), email.lower())
        self.rows[user.email] = (user, password_hash.hash(password))
        return user

    async def authenticate(self, email: str, password: str) -> User | None:
        user, hashed = self.rows.get(email.lower(), (None, DUMMY_HASH))
        ok = password_hash.verify(password, hashed)
        return user if user and ok else None

    async def get(self, user_id: str) -> User | None:
        return next((u for u, _ in self.rows.values() if u.id == user_id), None)

    async def delete(self, user_id: str) -> None:
        self.rows = {e: (u, h) for e, (u, h) in self.rows.items() if u.id != user_id}

    def add(self, user_id: str, password: str = "test-password") -> User:
        """An account with a known id, for tests that sign their own tokens."""
        user = User(user_id, f"{user_id}@example.com")
        self.rows[user.email] = (user, password_hash.hash(password))
        return user
