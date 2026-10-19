"""Endpoints for the platform, not users: liveness, readiness and Prometheus metrics. None is
authenticated or limited; keep /metrics off the public internet (Phase 9: the Ingress refuses
it, and Prometheus scrapes the pods directly)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from chatbot.api.dependencies import get_readiness

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(ready: Annotated[bool, Depends(get_readiness)]) -> JSONResponse:
    """The readiness probe: 503 until the database answers."""
    if ready:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not ready"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """For Prometheus (observability/metrics.py)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
