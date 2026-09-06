"""Device provisioning, claiming, and management payloads."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.core.device_security import normalise_claim_code

# ---------------------------------------------------------------------------
# Provisioning: device-facing, unauthenticated
# ---------------------------------------------------------------------------


class ProvisionRequest(BaseModel):
    """A device announcing itself before it has any credential.

    ``hardware_id`` identifies the board; it is not a secret and is never
    treated as one. It is printed on the chip and trivially spoofed, so it
    determines *which row* is provisioned, never *who owns it*.
    """

    model_config = ConfigDict(extra="forbid")

    hardware_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")
    model: str = Field(min_length=1, max_length=64)
    firmware_version: str | None = Field(default=None, max_length=32)


class ProvisionResponse(BaseModel):
    """What the device shows on its face, and what it keeps to itself."""

    device_id: uuid.UUID
    # Displayed on the AMOLED for the user to type.
    claim_code: str
    # Never displayed. Proves later requests come from this same device.
    provisioning_token: str
    expires_at: datetime
    poll_interval_seconds: int


class ProvisionPollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provisioning_token: str = Field(min_length=1, max_length=512)


class ProvisionPendingResponse(BaseModel):
    """Nobody has claimed the code yet."""

    status: Literal["pending"] = "pending"
    expires_at: datetime
    poll_interval_seconds: int


class ProvisionCompleteResponse(BaseModel):
    """The device has an owner. Returned exactly once.

    ``device_token`` is shown here and never again; the server keeps only a
    digest. A device that loses it must be re-provisioned.
    """

    status: Literal["claimed"] = "claimed"
    device_id: uuid.UUID
    device_token: str
    device_name: str | None
    heartbeat_interval_seconds: int


# ---------------------------------------------------------------------------
# Claiming: user-facing, authenticated
# ---------------------------------------------------------------------------


class ClaimRequest(BaseModel):
    """A user adopting the device showing this code."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=80)

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        # People type lower case, drop the hyphen, or paste stray spaces.
        normalised = normalise_claim_code(value)
        if not normalised:
            raise ValueError("Claim code must not be blank.")
        return normalised

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# Device management
# ---------------------------------------------------------------------------


class DeviceRead(BaseModel):
    """A device as returned to its owner.

    ``is_online`` is derived from ``last_seen_at`` rather than stored, so it
    cannot go stale when a process dies without writing a final "offline".
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None
    model: str
    firmware_version: str | None
    hardware_id: str
    is_online: bool
    last_seen_at: datetime | None
    claimed_at: datetime | None
    created_at: datetime


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)


class TelemetryRead(BaseModel):
    """One stored telemetry event."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    recorded_at: datetime
    received_at: datetime
    battery_percent: int | None
    temperature_c: float | None
    distance_cm: int | None
    head_yaw: int | None
    head_pitch: int | None
    wifi_rssi: int | None
    uptime_seconds: int | None
    state: str | None
    payload: dict[str, Any]


class CommandAccepted(BaseModel):
    """A command was queued to a connected device.

    ``accepted`` means it was written to the socket, not that the servo has
    moved. The device reports completion separately via ``robot.result``.
    """

    command_id: uuid.UUID
    accepted: bool
    detail: str | None = None
