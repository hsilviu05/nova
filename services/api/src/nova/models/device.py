"""Device, credential, claim, and telemetry records.

A device moves through three states:

* **unprovisioned** -- no row exists, or a row exists with no owner
* **claimed** -- ``user_id`` and ``claimed_at`` are set
* **active** -- the device has collected a credential and connects

Ownership is established by the claim flow in
:mod:`nova.services.provisioning`, not by anything the device asserts about
itself. A ``hardware_id`` is an identifier, never a credential: it is printed
on the chip and trivially spoofed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nova.core.clock import utc_now
from nova.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nova.models.user import User

# A device is considered offline once no heartbeat has arrived for this long.
# Status is derived from ``last_seen_at`` rather than stored, so it cannot go
# stale when a process dies without writing a final "offline".
OFFLINE_AFTER = timedelta(seconds=90)


class Device(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One physical NOVA."""

    __tablename__ = "devices"

    # Null until claimed. An unclaimed device row exists so that a claim code
    # can point at something, but it belongs to nobody.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Derived from the ESP32 eFuse MAC. Stable across reflashes, which is what
    # makes re-provisioning after a factory reset find the same row.
    hardware_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)

    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[User | None] = relationship(back_populates="devices")
    credentials: Mapped[list[DeviceCredential]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    claims: Mapped[list[DeviceClaim]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_claimed(self) -> bool:
        return self.user_id is not None

    @property
    def is_online(self) -> bool:
        """True when a heartbeat arrived recently enough."""
        if self.last_seen_at is None:
            return False
        return utc_now() - self.last_seen_at < OFFLINE_AFTER

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Device id={self.id} hardware_id={self.hardware_id!r}>"


class DeviceCredential(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A long-lived token a device authenticates with.

    Stored as a SHA-256 digest for the same reason refresh tokens are: the
    plaintext is full-entropy random data, so a fast digest is the right
    trade, and a database leak yields nothing usable.

    A device may hold more than one live credential briefly, which is what
    makes rotation possible without a disconnect.
    """

    __tablename__ = "device_credentials"

    device_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(back_populates="credentials")

    __table_args__ = (
        # Only live credentials are ever looked up during authentication.
        Index(
            "ix_device_credentials_device_id_active",
            "device_id",
            postgresql_where="revoked_at IS NULL",
        ),
    )

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class DeviceClaim(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An in-progress claim: a code on the device's screen awaiting a user.

    Holds two independent secrets, both stored hashed:

    * ``code_hash`` -- the short code shown on the AMOLED and typed by the
      user. Low entropy by necessity, so it is short-lived, single-use, and
      attempt-capped.
    * ``provisioning_token_hash`` -- 256 bits, never displayed. Only the
      device that started provisioning can collect the resulting credential.

    Splitting them is the point: someone who reads the code off the screen can
    claim the device to their own account, but cannot impersonate the device.

    There is deliberately no per-claim attempt counter. Guesses are looked up
    by digest, so a wrong guess matches no row and could not be attributed to
    the code it was aiming at. Guessing is defended against by rate limiting
    the claim endpoint, which is the mechanism that can actually see the
    attempts.
    """

    __tablename__ = "device_claims"

    device_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    provisioning_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set when a user successfully claims the code.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the device collects its credential. Terminal.
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(back_populates="claims")

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= utc_now()

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def is_claimable(self) -> bool:
        """True only for a live code nobody has used yet."""
        return not self.is_consumed and not self.is_expired


class DeviceTelemetry(Base, UUIDPrimaryKeyMixin):
    """One structured telemetry event.

    Append-only, so there is no ``updated_at``. Common metrics get real
    columns because the analytics and ML pipelines aggregate over them;
    anything else lands in ``payload`` rather than growing the schema per
    event type.

    ``event_type`` is a plain string validated in the schema layer rather than
    a database enum: the set of event types grows with every firmware feature,
    and a native enum would make each addition a migration.
    """

    __tablename__ = "device_telemetry"

    # Neither column carries its own index. Every query that touches this
    # table is scoped to one device, so the composite indexes below cover
    # both, and a lone event_type index turned out to be actively harmful:
    # on a few million rows the planner AND-ed it into device-scoped
    # aggregates and spent most of each query walking it (docs/performance.md).
    device_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)

    # The device's own clock, which is what the ML pipeline treats as the
    # event time. Kept separate from arrival time so a queued batch uploaded
    # after a reconnect is not mistaken for a burst of activity.
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    battery_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_yaw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_pitch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wifi_rssi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uptime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String(24), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )

    __table_args__ = (
        # Every analytics query is "this device, most recent first"...
        Index("ix_device_telemetry_device_id_recorded_at", "device_id", "recorded_at"),
        # ...and the hourly, daily and heartbeat-gap ones are "this device,
        # this kind of event, most recent first". Index-only for all three.
        Index(
            "ix_device_telemetry_device_id_event_type_recorded_at",
            "device_id",
            "event_type",
            "recorded_at",
        ),
        CheckConstraint(
            "battery_percent IS NULL OR (battery_percent BETWEEN 0 AND 100)",
            name="battery_percent_range",
        ),
        CheckConstraint("distance_cm IS NULL OR distance_cm >= 0", name="distance_non_negative"),
        CheckConstraint(
            "uptime_seconds IS NULL OR uptime_seconds >= 0",
            name="uptime_non_negative",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DeviceTelemetry device_id={self.device_id} event={self.event_type!r}>"
