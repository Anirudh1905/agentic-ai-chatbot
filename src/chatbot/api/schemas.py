from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


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
    # Set when the run paused for approval (HumanInTheLoopMiddleware's request:
    # `action_requests` and `review_configs`); `reply` is then empty. Answer it with
    # POST /threads/{thread_id}/resume.
    pending: dict[str, Any] | None = None


class StreamEvent(StrEnum):
    """SSE event types. INTERRUPT means the run paused for approval (DONE follows). RESET means
    a model call failed partway through the reply and another (a retry, or the fallback model)
    starts it over: drop the tokens shown so far (Phase 10)."""

    TOKEN = "token"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    INTERRUPT = "interrupt"
    RESET = "reset"
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
    # Tool calls waiting for approval, if the thread is paused.
    pending: dict[str, Any] | None = None


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


class Memory(BaseModel):
    id: str
    content: str
    # "user" (about the user themselves) or "other" (an order, a trip, an open issue).
    scope: str
    topic: str
    updated_at: datetime


class MemoryList(BaseModel):
    # Newest first. Empty when the bot doesn't remember anything about the user yet.
    memories: list[Memory]


class EditedAction(BaseModel):
    name: str
    args: dict[str, Any]


class Decision(BaseModel):
    """One decision for HumanInTheLoopMiddleware, in the order of `action_requests`."""

    type: Literal["approve", "edit", "reject"]
    # For "edit": the call to run instead.
    edited_action: EditedAction | None = None
    # For "reject": why, passed to the model.
    message: str | None = None

    @model_validator(mode="after")
    def _edit_has_action(self) -> "Decision":
        if self.type == "edit" and self.edited_action is None:
            raise ValueError("an edit needs edited_action")
        return self


class ResumeRequest(BaseModel):
    # For a thread waiting for approval: one decision per pending tool call, in order.
    decisions: list[Decision] | None = None
    # Resume from (and fork at) an earlier checkpoint instead of the latest one.
    checkpoint_id: str | None = None


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
