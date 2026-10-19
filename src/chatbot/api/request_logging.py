import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chatbot.observability import request_id, thread_id

logger = logging.getLogger("chatbot.api")

# Tilt's readiness probe hits this every few seconds.
QUIET_PATHS = {"/healthz"}


class RequestLoggingMiddleware:
    """One log line per request, and a request ID on every log line written while serving it.

    Plain ASGI rather than BaseHTTPMiddleware so the context (and the timing) also covers the
    body of streaming responses, where the agent actually runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        rid = headers.get(b"x-request-id", b"").decode() or uuid.uuid4().hex[:8]
        request_id.set(rid)
        thread_id.set(None)
        status = 500
        start = time.monotonic()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            path = scope["path"]
            level = logging.DEBUG if path in QUIET_PATHS else logging.INFO
            # Set by the auth dependency once the token checks out (api/dependencies.py).
            user = scope.get("state", {}).get("user_id", "-")
            logger.log(
                level,
                "%s %s -> %d in %.2fs (user=%s)",
                scope["method"],
                path,
                status,
                time.monotonic() - start,
                user,
            )
