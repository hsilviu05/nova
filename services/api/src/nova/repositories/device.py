"""Device, credential, claim, and telemetry persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, desc, select, update

from nova.models.device import (
    Device,
    DeviceClaim,
    DeviceCredential,
    DeviceTelemetry,
)
from nova.repositories.base import BaseRepository


class DeviceRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.device.Device` rows."""

    async def get_by_id(self, device_id: uuid.UUID) -> Device | None:
        return await self._session.get(Device, device_id)

    async def get_for_owner(self, device_id: uuid.UUID, owner_id: uuid.UUID) -> Device | None:
        """Fetch a device only if ``owner_id`` owns it.

        Ownership is part of the query rather than a check afterwards, so a
        handler cannot forget to apply it and leak another user's device.
        """
        stmt = select(Device).where(Device.id == device_id, Device.user_id == owner_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_hardware_id(self, hardware_id: str) -> Device | None:
        stmt = select(Device).where(Device.hardware_id == hardware_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_owner(self, owner_id: uuid.UUID) -> list[Device]:
        stmt = select(Device).where(Device.user_id == owner_id).order_by(Device.created_at)
        return list((await self._session.execute(stmt)).scalars().all())

    def add(self, device: Device) -> Device:
        self._session.add(device)
        return device

    async def delete(self, device: Device) -> None:
        await self._session.delete(device)

    async def touch_last_seen(self, device_id: uuid.UUID, *, at: datetime) -> None:
        """Record a heartbeat.

        Issued as a targeted UPDATE rather than loading the row: this runs on
        every heartbeat from every device, and it never needs the entity.
        """
        stmt = update(Device).where(Device.id == device_id).values(last_seen_at=at)
        await self._session.execute(stmt)


class DeviceCredentialRepository(BaseRepository):
    """Reads and writes device authentication credentials."""

    async def get_live_by_hash(self, token_hash: str) -> DeviceCredential | None:
        """Find an unrevoked credential by digest."""
        stmt = select(DeviceCredential).where(
            DeviceCredential.token_hash == token_hash,
            DeviceCredential.revoked_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, credential: DeviceCredential) -> DeviceCredential:
        self._session.add(credential)
        return credential

    async def revoke_all_for_device(self, device_id: uuid.UUID, *, at: datetime) -> int:
        """Revoke every live credential for a device.

        Used when ownership changes and when a user removes a device, so an
        old credential cannot keep streaming telemetry to a former owner.
        """
        stmt = (
            update(DeviceCredential)
            .where(
                DeviceCredential.device_id == device_id,
                DeviceCredential.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)


class DeviceClaimRepository(BaseRepository):
    """Reads and writes in-progress claims."""

    async def get_claimable_by_code_hash(self, code_hash: str) -> DeviceClaim | None:
        """Find a claim by its code digest, consumed or not.

        Expired and already-consumed claims are returned too: the service
        distinguishes them, because "this code was already used" and "no such
        code" need different handling even though both fail.
        """
        stmt = select(DeviceClaim).where(DeviceClaim.code_hash == code_hash)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_provisioning_token_hash(self, token_hash: str) -> DeviceClaim | None:
        stmt = select(DeviceClaim).where(DeviceClaim.provisioning_token_hash == token_hash)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, claim: DeviceClaim) -> DeviceClaim:
        self._session.add(claim)
        return claim

    async def expire_open_claims_for_device(self, device_id: uuid.UUID, *, at: datetime) -> int:
        """Retire any outstanding claim for a device.

        Called when provisioning restarts, so a code still readable on a
        previous screen cannot be used after a new one is issued.
        """
        stmt = (
            update(DeviceClaim)
            .where(
                DeviceClaim.device_id == device_id,
                DeviceClaim.consumed_at.is_(None),
                DeviceClaim.expires_at > at,
            )
            .values(expires_at=at)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)


class DeviceTelemetryRepository(BaseRepository):
    """Writes and reads telemetry events."""

    def add(self, event: DeviceTelemetry) -> DeviceTelemetry:
        self._session.add(event)
        return event

    def add_all(self, events: list[DeviceTelemetry]) -> None:
        """Stage a batch, as sent after an offline period."""
        self._session.add_all(events)

    async def list_for_device(
        self,
        device_id: uuid.UUID,
        *,
        limit: int = 100,
        event_type: str | None = None,
    ) -> list[DeviceTelemetry]:
        """Most recent events first."""
        stmt = select(DeviceTelemetry).where(DeviceTelemetry.device_id == device_id)
        if event_type is not None:
            stmt = stmt.where(DeviceTelemetry.event_type == event_type)
        stmt = stmt.order_by(desc(DeviceTelemetry.recorded_at)).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())
