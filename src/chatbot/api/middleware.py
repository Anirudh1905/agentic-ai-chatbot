import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chatbot.observability.logs import request_id, thread_id
from chatbot.observability.metrics import REQUEST_SECONDS, REQUESTS

logger = logging.getLogger("chatbot.api")

# Probes hit these every few seconds, and Prometheus scrapes /metrics.
QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}


class RequestMiddleware:
    """One log line and the metrics per request, and a request ID on every log line written while
    serving it.

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
            elapsed = time.monotonic() - start
            if path not in QUIET_PATHS:
                # The route's template (/threads/{thread_id}), so each label has few values.
                route = getattr(scope.get("route"), "path", "unmatched")
                REQUESTS.labels(scope["method"], route, str(status)).inc()
                REQUEST_SECONDS.labels(scope["method"], route).observe(elapsed)
            level = logging.DEBUG if path in QUIET_PATHS else logging.INFO
            # Set by `limited_user` once the token checks out (api/security.py); "-" without a
            # user, and for /auth/me, which doesn't take that dependency.
            user = scope.get("state", {}).get("user_id", "-")
            logger.log(
                level,
                "%s %s -> %d in %.2fs (user=%s)",
                scope["method"],
                path,
                status,
                elapsed,
                user,
            )
