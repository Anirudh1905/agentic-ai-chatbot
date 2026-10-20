"""One memory push, as a queue message.

Three kinds, each carrying what its push needs:
- `summary`: the turns a summary folded away. They've left the thread, so the job carries them
  (tool results blanked: the memory writer never reads them, and they'd make jobs large).
- `idle`: a thread idle past MEMORY_THREAD_TTL_MINUTES; the worker reads it and pushes what's
  after its cursor.
- `remember`: an exchange the user asked to remember (the button, or the `remember` tool).

Everything was redacted before it reached the thread (Phase 5), so no raw PII is queued.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from langchain_core.messages import AnyMessage, ToolMessage, messages_from_dict, messages_to_dict

Kind = Literal["summary", "idle", "remember"]


@dataclass(frozen=True)
class MemoryPush:
    kind: Kind
    user_id: str
    thread_id: str | None = None
    messages: str | None = None  # summary: the folded-away messages, as LangChain dicts (JSON)
    thread_updated_at: str | None = None  # idle: the thread's activity this push covers
    question: str | None = None  # remember
    reply: str | None = None  # remember

    @classmethod
    def summary(cls, user_id: str, thread_id: str, messages: Sequence[AnyMessage]) -> "MemoryPush":
        kept = [
            m.model_copy(update={"content": ""}) if isinstance(m, ToolMessage) else m
            for m in messages
        ]
        return cls("summary", user_id, thread_id, messages=json.dumps(messages_to_dict(kept)))

    @classmethod
    def idle(cls, user_id: str, thread_id: str, thread_updated_at: str) -> "MemoryPush":
        return cls("idle", user_id, thread_id, thread_updated_at=thread_updated_at)

    @classmethod
    def remember(cls, user_id: str, question: str | None, reply: str | None) -> "MemoryPush":
        return cls("remember", user_id, question=question, reply=reply)

    def message_list(self) -> list[AnyMessage]:
        if not self.messages:
            return []
        return cast(list[AnyMessage], messages_from_dict(json.loads(self.messages)))

    @property
    def group_id(self) -> str:
        """SQS FIFO runs one group's messages in order, one at a time: one user's pushes."""
        return self.user_id

    @property
    def dedupe_id(self) -> str:
        """The same push enqueued twice (within SQS's 5-minute window) runs once."""
        if self.kind == "summary":
            ids = [m.id for m in self.message_list()]
            key = ids[-1] if ids and ids[-1] else self.messages or ""
        elif self.kind == "idle":
            key = self.thread_updated_at or ""
        else:
            key = f"{self.question}|{self.reply}"
        digest = hashlib.sha256(f"{self.user_id}|{self.thread_id}|{key}".encode()).hexdigest()
        return f"{self.kind}-{digest}"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, body: str) -> "MemoryPush":
        return cls(**json.loads(body))
