"""A model gateway with an outage (Phase 12, make outage-test): chat completions for the models in
FAIL_MODELS answer 503 at once; every other request (other models, embeddings) goes through to
the real gateway, streamed. Runs in the cluster from the app image, which has Starlette, httpx
and uvicorn; the upstream URL is the app's own secret.

GET /_outage counts what was failed and passed.
"""

import contextlib
import json
import os
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

UPSTREAM = Path("/run/secrets/OPENAI_BASE_URL").read_text().strip().rstrip("/")
FAIL_MODELS = set(os.environ.get("FAIL_MODELS", "gpt-5.4").split(","))
# Not forwarded: the client's connection details, and its encodings, so the body passes as is.
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}

client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
counts: Counter[str] = Counter()


async def stats(request: Request) -> Response:
    return JSONResponse(dict(counts))


async def proxy(request: Request) -> Response:
    body = await request.body()
    model = None
    if request.url.path.endswith("/chat/completions"):
        with contextlib.suppress(ValueError):
            model = json.loads(body).get("model")
    if model in FAIL_MODELS:
        counts[f"failed {model}"] += 1
        return JSONResponse(
            {"error": {"message": f"injected outage for {model}", "type": "server_error"}},
            status_code=503,
        )
    counts[f"passed {model or request.url.path}"] += 1
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    query = f"?{request.url.query}" if request.url.query else ""
    upstream = await client.send(
        client.build_request(
            request.method, UPSTREAM + request.url.path + query, headers=headers, content=body
        ),
        stream=True,
    )

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    skip = HOP_HEADERS | {"content-encoding"}
    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in skip},
    )


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
app = Starlette(
    routes=[
        Route("/_outage", stats),
        Route("/{path:path}", proxy, methods=METHODS),
    ]
)
