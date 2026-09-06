"""Device provisioning and management routes.

Two audiences, deliberately separated:

* ``/devices/provision*`` is called by hardware that has no credential yet, so
  it is unauthenticated and tightly rate limited.
* everything else is called by a signed-in user and scoped to devices they
  own.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request, status

from nova.api.deps import (
    CurrentUser,
    get_app_settings,
    get_device_service,
    get_provisioning_service,
    get_rate_limiter,
    rate_limit_key,
)
from nova.core.config import Settings
from nova.schemas.device import (
    ClaimRequest,
    CommandAccepted,
    DeviceRead,
    DeviceUpdate,
    ProvisionCompleteResponse,
    ProvisionPendingResponse,
    ProvisionPollRequest,
    ProvisionRequest,
    ProvisionResponse,
    TelemetryRead,
)
from nova.schemas.protocol import OutboundCommand
from nova.services.device import DeviceService
from nova.services.provisioning import ProvisioningService
from nova.services.rate_limit import RateLimiter

router = APIRouter(prefix="/devices", tags=["devices"])

ProvisioningDep = Annotated[ProvisioningService, Depends(get_provisioning_service)]
DeviceServiceDep = Annotated[DeviceService, Depends(get_device_service)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# ---------------------------------------------------------------------------
# Device-facing: no credential yet
# ---------------------------------------------------------------------------


@router.post(
    "/provision",
    response_model=ProvisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start provisioning (device-facing)",
)
async def provision(
    payload: ProvisionRequest,
    request: Request,
    provisioning: ProvisioningDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> ProvisionResponse:
    """Register an unprovisioned device and return the code it should display.

    Unauthenticated by necessity -- the device has no credential yet -- so it
    is rate limited by address. Provisioning is a rare event, and the limit
    reflects that.

    Calling this again retires any previous code for the same hardware, so a
    code still readable on an earlier screen cannot be used afterwards.
    """
    await limiter.check(
        rate_limit_key(request, scope="provision"),
        limit=settings.device.provision_rate_limit_attempts,
        window_seconds=settings.device.provision_rate_limit_window_seconds,
    )
    return await provisioning.provision(
        hardware_id=payload.hardware_id,
        model=payload.model,
        firmware_version=payload.firmware_version,
    )


@router.post(
    "/provision/poll",
    response_model=ProvisionPendingResponse | ProvisionCompleteResponse,
    summary="Collect credentials once claimed (device-facing)",
    responses={401: {"description": "Provisioning token is invalid or spent."}},
)
async def provision_poll(
    payload: ProvisionPollRequest,
    provisioning: ProvisioningDep,
) -> ProvisionPendingResponse | ProvisionCompleteResponse:
    """Return the device credential once a user has claimed the code.

    Returns ``pending`` until then. The credential is returned exactly once;
    a second successful collection is refused, because a repeat means either
    a device bug or a stolen provisioning token.
    """
    return await provisioning.collect(provisioning_token=payload.provisioning_token)


# ---------------------------------------------------------------------------
# User-facing
# ---------------------------------------------------------------------------


@router.post(
    "/claim",
    response_model=DeviceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Claim the device showing a code",
    responses={404: {"description": "Code is unknown, expired, or already used."}},
)
async def claim(
    payload: ClaimRequest,
    request: Request,
    current_user: CurrentUser,
    provisioning: ProvisioningDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> DeviceRead:
    """Adopt a device by typing the code on its screen.

    Rate limited per user: a claim code is short enough to be guessable in
    principle, and this endpoint is the only place a wrong guess is
    observable, since a guess that matches no code matches no row either.
    """
    await limiter.check(
        rate_limit_key(request, scope="claim", identifier=str(current_user.id)),
        limit=settings.device.claim_rate_limit_attempts,
        window_seconds=settings.device.claim_rate_limit_window_seconds,
    )
    return await provisioning.claim(user=current_user, code=payload.code, name=payload.name)


@router.get("", response_model=list[DeviceRead], summary="List your devices")
async def list_devices(current_user: CurrentUser, devices: DeviceServiceDep) -> list[DeviceRead]:
    return await devices.list_for_owner(current_user.id)


@router.get("/{device_id}", response_model=DeviceRead, summary="Device detail")
async def read_device(
    device_id: uuid.UUID, current_user: CurrentUser, devices: DeviceServiceDep
) -> DeviceRead:
    """Return one device.

    A device belonging to someone else is reported as not found, not as
    forbidden, so the endpoint cannot confirm that an id exists.
    """
    return await devices.get_for_owner(device_id, current_user.id)


@router.patch("/{device_id}", response_model=DeviceRead, summary="Rename a device")
async def update_device(
    device_id: uuid.UUID,
    payload: DeviceUpdate,
    current_user: CurrentUser,
    devices: DeviceServiceDep,
) -> DeviceRead:
    return await devices.update_for_owner(device_id, current_user.id, payload)


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Remove a device",
)
async def delete_device(
    device_id: uuid.UUID,
    current_user: CurrentUser,
    provisioning: ProvisioningDep,
) -> None:
    """Unclaim and delete a device.

    Credentials are revoked before the row is removed, so an in-flight
    connection cannot outlive the device it belongs to.
    """
    await provisioning.release(user=current_user, device_id=device_id)


@router.get(
    "/{device_id}/telemetry",
    response_model=list[TelemetryRead],
    summary="Recent telemetry",
)
async def read_telemetry(
    device_id: uuid.UUID,
    current_user: CurrentUser,
    devices: DeviceServiceDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    event_type: Annotated[str | None, Query(max_length=48)] = None,
) -> list[TelemetryRead]:
    """Most recent telemetry first."""
    return await devices.list_telemetry(
        device_id, current_user.id, limit=limit, event_type=event_type
    )


@router.post(
    "/{device_id}/commands",
    response_model=CommandAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a command to a device",
    responses={503: {"description": "Device is not connected."}},
)
async def send_command(
    device_id: uuid.UUID,
    command: Annotated[OutboundCommand, Body()],
    current_user: CurrentUser,
    devices: DeviceServiceDep,
) -> CommandAccepted:
    """Dispatch a command over the device's live WebSocket.

    202, not 200: acceptance means the frame reached the socket, not that the
    servo has moved. The device reports completion separately.

    Commands are not queued for an offline device. A head movement delivered
    minutes later, out of context, is worse than one that never arrives.
    """
    return await devices.send_command(device_id, current_user.id, command)
