from enum import StrEnum

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    # Accepted now so the contract doesn't change; persisted from Phase 3 onward.
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: str | None = None


class StreamEvent(StrEnum):
    """SSE event types. Only TOKEN, DONE and ERROR are emitted in Phase 1."""

    TOKEN = "token"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    INTERRUPT = "interrupt"
    DONE = "done"
    ERROR = "error"
