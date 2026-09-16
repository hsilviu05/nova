"""telemetry indexes

Revision ID: a3f9c2d1b7e4
Revises: 8c1f2a7d4e90
Create Date: 2026-09-08 09:00:00.000000+00:00

Replaces the two single-column indexes on device_telemetry with one
composite. Every query against the table is scoped to a device, so the
device_id index was redundant with (device_id, recorded_at), and the
event_type index was worse than redundant: with a few million rows the
planner AND-ed it into the device-scoped aggregates and spent most of each
query walking it. Measured in docs/performance.md.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a3f9c2d1b7e4"
down_revision: str | None = "8c1f2a7d4e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_device_telemetry_device_id_event_type_recorded_at",
        "device_telemetry",
        ["device_id", "event_type", "recorded_at"],
    )
    op.drop_index("ix_device_telemetry_event_type", table_name="device_telemetry")
    op.drop_index("ix_device_telemetry_device_id", table_name="device_telemetry")


def downgrade() -> None:
    op.create_index("ix_device_telemetry_device_id", "device_telemetry", ["device_id"])
    op.create_index("ix_device_telemetry_event_type", "device_telemetry", ["event_type"])
    op.drop_index(
        "ix_device_telemetry_device_id_event_type_recorded_at", table_name="device_telemetry"
    )
