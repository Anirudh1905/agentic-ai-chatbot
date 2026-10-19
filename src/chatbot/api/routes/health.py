"""Endpoints for the platform, not users: liveness and Prometheus metrics. Neither is
authenticated or limited; keep /metrics off the public internet (Phase 9: only the cluster
network reaches it)."""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """For Prometheus (observability/metrics.py)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
