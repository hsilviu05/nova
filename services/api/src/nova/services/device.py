"""Device authentication, telemetry ingestion, and command dispatch."""

from __future__ import annotations

import uuid

from nova.core.clock import utc_now
from nova.core.device_security import hash_device_token
from nova.core.errors import AuthenticationError, NotFoundError, ServiceUnavailableError
from nova.core.logging import get_logger
from nova.models.device import Device, DeviceTelemetry
from nova.repositories.device import (
    DeviceCredentialRepository,
    DeviceRepository,
    DeviceTelemetryRepository,
)
from nova.schemas.device import CommandAccepted, DeviceRead, DeviceUpdate, TelemetryRead
from nova.schemas.protocol import (
    HeartbeatPayload,
    OutboundCommand,
    TelemetryEventPayload,
)
from nova.services.connections import ConnectionRegistry

logger = get_logger(__name__)

# Telemetry the device emits alongside a heartbeat, recorded as its own event
# so vitals are queryable on the same footing as everything else.
HEARTBEAT_EVENT = "heartbeat"


class DeviceService:
    """Owns device authentication and the data a device produces."""

    def __init__(
        self,
        *,
        devices: DeviceRepository,
        credentials: DeviceCredentialRepository,
        telemetry: DeviceTelemetryRepository,
        connections: ConnectionRegistry,
    ) -> None:
        self._devices = devices
        self._credentials = credentials
        self._telemetry = telemetry
        self._connections = connections

    # -- authentication ---------------------------------------------------

    async def authenticate(self, token: str) -> Device:
        """Resolve a device token to its device.

        Raises:
            AuthenticationError: if the credential is unknown, revoked, or
                its device has since been deleted or unclaimed.
        """
        credential = await self._credentials.get_live_by_hash(hash_device_token(token))
        if credential is None:
            logger.info("device_auth_failed", reason="unknown_credential")
            raise self._invalid_credential()

        device = await self._devices.get_by_id(credential.device_id)
        if device is None or not device.is_claimed:
            # Unclaimed means the device was released or re-provisioned; the
            # credential should not outlive that.
            logger.info(
                "device_auth_failed",
                reason="device_unavailable",
                device_id=str(credential.device_id),
            )
            raise self._invalid_credential()

        credential.last_used_at = utc_now()
        return device

    # -- ingestion --------------------------------------------------------

    async def record_heartbeat(self, device: Device, payload: HeartbeatPayload) -> None:
        """Record a heartbeat and refresh the device's liveness."""
        now = utc_now()
        await self._devices.touch_last_seen(device.id, at=now)

        if payload.firmware_version is not None:
            device.firmware_version = payload.firmware_version

        self._telemetry.add(
            DeviceTelemetry(
                device_id=device.id,
                event_type=HEARTBEAT_EVENT,
                recorded_at=now,
                received_at=now,
                battery_percent=payload.battery_percent,
                temperature_c=payload.temperature_c,
                wifi_rssi=payload.wifi_rssi,
                uptime_seconds=payload.uptime_seconds,
                state=payload.state,
                payload={},
            )
        )

    async def record_events(self, device: Device, events: list[TelemetryEventPayload]) -> int:
        """Store telemetry events. Returns how many were written."""
        now = utc_now()
        self._telemetry.add_all(
            [
                DeviceTelemetry(
                    device_id=device.id,
                    event_type=event.event_type,
                    recorded_at=event.recorded_at,
                    received_at=now,
                    battery_percent=event.battery_percent,
                    temperature_c=event.temperature_c,
                    distance_cm=event.distance_cm,
                    head_yaw=event.head_yaw,
                    head_pitch=event.head_pitch,
                    wifi_rssi=event.wifi_rssi,
                    uptime_seconds=event.uptime_seconds,
                    state=event.state,
                    payload=event.data,
                )
                for event in events
            ]
        )
        await self._devices.touch_last_seen(device.id, at=now)
        return len(events)

    # -- owner-facing operations ------------------------------------------

    async def list_for_owner(self, owner_id: uuid.UUID) -> list[DeviceRead]:
        devices = await self._devices.list_for_owner(owner_id)
        return [DeviceRead.model_validate(device) for device in devices]

    async def get_for_owner(self, device_id: uuid.UUID, owner_id: uuid.UUID) -> DeviceRead:
        return DeviceRead.model_validate(await self._require_owned(device_id, owner_id))

    async def update_for_owner(
        self, device_id: uuid.UUID, owner_id: uuid.UUID, payload: DeviceUpdate
    ) -> DeviceRead:
        device = await self._require_owned(device_id, owner_id)
        if payload.name is not None:
            device.name = payload.name.strip()
        return DeviceRead.model_validate(device)

    async def list_telemetry(
        self,
        device_id: uuid.UUID,
        owner_id: uuid.UUID,
        *,
        limit: int,
        event_type: str | None,
    ) -> list[TelemetryRead]:
        await self._require_owned(device_id, owner_id)
        events = await self._telemetry.list_for_device(
            device_id, limit=limit, event_type=event_type
        )
        return [TelemetryRead.model_validate(event) for event in events]

    async def send_command(
        self, device_id: uuid.UUID, owner_id: uuid.UUID, command: OutboundCommand
    ) -> CommandAccepted:
        """Dispatch a command to a connected device.

        Raises:
            NotFoundError: if the device is not the caller's.
            ServiceUnavailableError: if the device is not currently connected.
                Commands are not queued: a head movement that arrives when
                the device reconnects minutes later is worse than one that
                never arrives.
        """
        await self._require_owned(device_id, owner_id)

        delivered = await self._connections.send(device_id, command.model_dump(mode="json"))
        if not delivered:
            raise ServiceUnavailableError("Device is not connected.", code="device_offline")

        logger.info(
            "device_command_sent",
            device_id=str(device_id),
            command=command.command,
            command_id=str(command.id),
        )
        return CommandAccepted(command_id=command.id, accepted=True)

    # -- internals --------------------------------------------------------

    async def _require_owned(self, device_id: uuid.UUID, owner_id: uuid.UUID) -> Device:
        """Fetch a device the caller owns, or raise.

        Ownership is part of the query, so another user's device is
        indistinguishable from one that does not exist.
        """
        device = await self._devices.get_for_owner(device_id, owner_id)
        if device is None:
            raise NotFoundError("Device not found.", code="device_not_found")
        return device

    @staticmethod
    def _invalid_credential() -> AuthenticationError:
        return AuthenticationError(
            "Device credential is invalid.", code="invalid_device_credential"
        )
