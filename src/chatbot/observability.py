import hashlib
import hmac
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from chatbot.config import RunContext, Settings

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.types import TraceContext

# Set per request (see api/request_logging.py), so every log line from that request, including
# the graph and tools running under it, says which request and thread it belongs to.
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
thread_id: ContextVar[str | None] = ContextVar("thread_id", default=None)

# Chatty at INFO: one line per outbound HTTP call, per access, per health probe.
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "openai", "uvicorn.access", "ddgs", "primp")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        parts = []
        if rid := request_id.get():
            parts.append(f"req={rid}")
        if tid := thread_id.get():
            parts.append(f"thread={tid[:8]}")
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return True


class AppHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Our console handler, so configure_logging can replace it without removing others."""


def configure_logging(settings: Settings) -> None:
    handler = AppHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s%(ctx)s: %(message)s")
    )
    root = logging.getLogger()
    # Replace only our own handler (it's set up again on each app start), and leave the
    # ones others added, such as a deployment's log shipper or pytest's capture.
    root.handlers = [h for h in root.handlers if not isinstance(h, AppHandler)] + [handler]
    root.setLevel(settings.log_level)
    # LOG_LEVEL=DEBUG shows everything, including these.
    if root.level > logging.DEBUG:
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)


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
