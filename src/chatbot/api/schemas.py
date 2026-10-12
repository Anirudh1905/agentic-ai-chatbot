from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    # Accepted now so the contract doesn't change; persisted from Phase 3 onward.
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: str | None = None
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
