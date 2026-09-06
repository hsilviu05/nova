"""Health and readiness routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from nova import __version__
from nova.api.deps import get_app_settings, get_health_service
from nova.core.config import Settings
from nova.schemas.health import HealthResponse, ReadinessResponse
from nova.services.health import HealthService

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def health(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> HealthResponse:
    """Report that the process is up.

    Deliberately touches no dependency: a database outage must not make the
    orchestrator kill an otherwise healthy container.
    """
    return HealthResponse(version=__version__, environment=settings.environment)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"description": "One or more dependencies are unreachable."}},
)
async def ready(
    response: Response,
    health_service: Annotated[HealthService, Depends(get_health_service)],
) -> ReadinessResponse:
    """Report whether NOVA can serve traffic.

    Returns 503 when Postgres or Redis is unreachable so a load balancer stops
    routing to this instance.
    """
    result = await health_service.check_readiness()
    if not result.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
