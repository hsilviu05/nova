"""Analytics routes.

Nested under a device because that is what the numbers are about. Ownership
is checked before any aggregation runs, using the device service's own check
rather than a second copy of it.

The window is capped rather than free: each request is a scan over telemetry,
and an unbounded parameter is an invitation to ask for five years of it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from nova.api.deps import CurrentUser, get_analytics_service, get_device_service
from nova.schemas.analytics import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
    AnalyticsRead,
    InsightsRead,
)
from nova.services.analytics import AnalyticsService
from nova.services.device import DeviceService

router = APIRouter(prefix="/devices", tags=["analytics"])

AnalyticsDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
DeviceServiceDep = Annotated[DeviceService, Depends(get_device_service)]

WindowQuery = Annotated[
    int,
    Query(
        ge=MIN_WINDOW_DAYS,
        le=MAX_WINDOW_DAYS,
        description="Days of telemetry to aggregate, in the owner's timezone.",
    ),
]


@router.get(
    "/{device_id}/analytics",
    response_model=AnalyticsRead,
    summary="Aggregated telemetry for the insights screen",
    responses={404: {"description": "Device not found."}},
)
async def read_analytics(
    device_id: uuid.UUID,
    current_user: CurrentUser,
    devices: DeviceServiceDep,
    analytics: AnalyticsDep,
    window_days: WindowQuery = DEFAULT_WINDOW_DAYS,
) -> AnalyticsRead:
    """Everything the insights screen plots, bucketed in the owner's timezone.

    One response rather than six endpoints: the screen needs all of it before
    it can render anything, and separate requests would be separate round
    trips and separate chances for the panels to disagree about the window.
    """
    await devices.require_owned(device_id, current_user.id)
    return await analytics.overview(
        device_id, window_days=window_days, timezone=current_user.timezone
    )


@router.get(
    "/{device_id}/insights",
    response_model=InsightsRead,
    summary="What the telemetry supports saying",
    responses={404: {"description": "Device not found."}},
)
async def read_insights(
    device_id: uuid.UUID,
    current_user: CurrentUser,
    devices: DeviceServiceDep,
    analytics: AnalyticsDep,
    window_days: WindowQuery = DEFAULT_WINDOW_DAYS,
) -> InsightsRead:
    """Derived statements, each carrying the evidence behind it.

    An empty ``insights`` list with an ``insufficient_reason`` is a normal
    response, not an error: NOVA declining to guess is the feature working.
    """
    await devices.require_owned(device_id, current_user.id)
    return await analytics.insights(
        device_id, window_days=window_days, timezone=current_user.timezone
    )
