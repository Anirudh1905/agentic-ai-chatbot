"""Endpoints for the platform, not users: liveness, readiness and Prometheus metrics. None is
authenticated or limited; keep /metrics off the public internet (Phase 9: in the cluster it's on
its own port, METRICS_PORT, which the gateway doesn't route; Prometheus scrapes the pods)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from chatbot.api.dependencies import get_readiness
from chatbot.config import Settings, get_settings

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
def metrics(settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    """For Prometheus (observability/metrics.py), unless METRICS_PORT moved them off this port."""
    if settings.metrics_port:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
