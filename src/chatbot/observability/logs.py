"""Logging: one console format, with the request and thread on every line."""

import logging
from contextvars import ContextVar

from chatbot.config import Settings

# Set per request (see api/middleware.py), so every log line from that request, including
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
