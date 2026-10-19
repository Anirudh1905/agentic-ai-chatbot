"""Tracing in Langfuse: the client, the callbacks a run passes to be traced, and the metadata
that names its trace, session (the thread), user (hashed) and tags."""

import hashlib
import hmac
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from chatbot.config import RunContext, Settings

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.types import TraceContext


def langfuse_client(settings: Settings) -> "Langfuse | None":
    """The Langfuse client, set up once; None unless the LANGFUSE keys are set."""
    if not settings.tracing_enabled:
        return None
    from langfuse import Langfuse

    assert settings.langfuse_secret_key is not None
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
    )


def build_callbacks(settings: Settings, trace_id: str | None = None) -> list[BaseCallbackHandler]:
    """What a run passes as `callbacks` to be traced: none unless tracing is on. With
    `trace_id`, the run's trace gets that id, so scores can be sent to it later (the evals)."""
    if langfuse_client(settings) is None:
        return []
    from langfuse.langchain import CallbackHandler

    context: TraceContext | None = {"trace_id": trace_id} if trace_id else None
    return [CallbackHandler(public_key=settings.langfuse_public_key, trace_context=context)]


def hashed_user(user_id: str, settings: Settings) -> str:
    """The user as traces see them: an HMAC of their id with JWT_SECRET, so a trace can be
    matched to a user only by someone who has the secret (a plain hash of "alice" isn't)."""
    key = settings.jwt_secret.get_secret_value().encode() if settings.jwt_secret else b""
    return hmac.new(key, user_id.encode(), hashlib.sha256).hexdigest()[:16]


def trace_metadata(
    name: str, thread_id: UUID, context: RunContext, settings: Settings
) -> dict[str, Any]:
    """A run's metadata: Langfuse reads the `langfuse_` keys as the trace's name, session (the
    thread, so a conversation's runs group together), user and tags, and keeps the rest. The
    model, tokens, cost, tool calls and approval pauses it records from the run itself."""
    tags = [name] + (["own-key"] if context.credentials else [])
    return {
        "langfuse_trace_name": name,
        "langfuse_session_id": str(thread_id),
        "langfuse_user_id": hashed_user(context.user_id, settings),
        "langfuse_tags": tags,
        "model": settings.model,
    }
