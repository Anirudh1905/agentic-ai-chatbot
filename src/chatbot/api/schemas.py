from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    # Omit to start a new thread. An unknown ID creates a thread with that ID, so clients can
    # generate their own; someone else's ID is a 404.
    thread_id: UUID | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: UUID
    # Knowledge base chunks and web pages the tools returned for this answer.
    sources: list[dict[str, Any]] = Field(default_factory=list)


class StreamEvent(StrEnum):
    """SSE event types. INTERRUPT is emitted from Phase 4."""

    TOKEN = "token"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    INTERRUPT = "interrupt"
    DONE = "done"
    ERROR = "error"


class CreateThreadRequest(BaseModel):
    title: str | None = None


class ThreadInfo(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    # For assistant messages: tools called and sources returned since the user's message.
    tools: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)


class ThreadMessages(BaseModel):
    thread_id: UUID
    # Set once older messages have been condensed; they are no longer in `messages`.
    summary: str | None = None
    messages: list[ChatMessage]


class CheckpointInfo(BaseModel):
    checkpoint_id: str
    parent_checkpoint_id: str | None
    created_at: str | None
    # "input" (new message), "loop" (a node ran), "update" (state edited), "fork".
    source: str | None
    step: int | None
    # Nodes that run next if the thread is resumed from here. Empty means the run finished.
    next: list[str]
    message_count: int
    last_message: str | None


class ResumeRequest(BaseModel):
    # Resume from (and fork at) an earlier checkpoint instead of the latest one.
    checkpoint_id: str | None = None
