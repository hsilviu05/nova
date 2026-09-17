"""Alert routes: what the watcher noticed, and clearing it."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from nova.api.deps import CurrentUser, get_alert_repository
from nova.core.clock import utc_now
from nova.core.errors import NotFoundError
from nova.repositories.alert import AlertRepository
from nova.schemas.alert import AcknowledgedCount, AlertPage, AlertRead

router = APIRouter(prefix="/alerts", tags=["alerts"])

AlertsDep = Annotated[AlertRepository, Depends(get_alert_repository)]


@router.get("", response_model=AlertPage, summary="What the watcher has noticed")
async def list_alerts(
    current_user: CurrentUser,
    alerts: AlertsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    unacknowledged: Annotated[bool, Query()] = False,
) -> AlertPage:
    """Newest first. Server-wide: projects are server configuration."""
    rows = await alerts.list_recent(limit=limit, unacknowledged_only=unacknowledged)
    return AlertPage(
        items=[AlertRead.model_validate(row) for row in rows],
        unacknowledged=await alerts.count_unacknowledged(),
    )


@router.post("/acknowledge-all", response_model=AcknowledgedCount, summary="Clear every alert")
async def acknowledge_all(current_user: CurrentUser, alerts: AlertsDep) -> AcknowledgedCount:
    return AcknowledgedCount(acknowledged=await alerts.acknowledge_all(at=utc_now()))


@router.post("/{alert_id}/acknowledge", response_model=AlertRead, summary="Clear one alert")
async def acknowledge(
    current_user: CurrentUser, alerts: AlertsDep, alert_id: uuid.UUID
) -> AlertRead:
    alert = await alerts.acknowledge(alert_id, at=utc_now())
    if alert is None:
        raise NotFoundError("No such alert.", code="alert_not_found")
    return AlertRead.model_validate(alert)
