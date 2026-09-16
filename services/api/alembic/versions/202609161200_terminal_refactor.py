"""Drop the device tables, add the tool audit log.

NOVA stopped being a physical robot. The four device tables described a piece
of hardware that no longer exists -- provisioning, credentials, claim codes,
and telemetry from sensors -- and nothing replaces them one-for-one. What
replaces them *conceptually* is ``tool_invocations``: the record of what NOVA
did, which is what the dashboard reads and what an audit reads.

The downgrade rebuilds the device schema exactly, because a migration that
cannot be reversed is a migration nobody can deploy with confidence. It does
not restore the rows: the data went with the tables, and pretending otherwise
in a docstring would be worse than saying so here.

Revision ID: 4f0c1ab7d9e2
Revises: d5949ac01523
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4f0c1ab7d9e2"
down_revision: str | None = "d5949ac01523"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("tool_group", sa.String(length=24), nullable=False),
        sa.Column("permission", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("initiated_by_model", sa.Boolean(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name=op.f("ck_tool_invocations_duration_non_negative"),
        ),
        sa.CheckConstraint(
            "permission IN ('read', 'write', 'destructive')",
            name=op.f("ck_tool_invocations_permission_known"),
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'refused', 'timed_out')",
            name=op.f("ck_tool_invocations_status_known"),
        ),
        # SET NULL, not CASCADE: deleting a conversation must not erase the
        # record of what was run while it was open.
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_tool_invocations_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tool_invocations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_invocations")),
    )
    op.create_index(
        op.f("ix_tool_invocations_created_at"), "tool_invocations", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_tool_invocations_tool_name"), "tool_invocations", ["tool_name"], unique=False
    )
    op.create_index(
        op.f("ix_tool_invocations_user_id"), "tool_invocations", ["user_id"], unique=False
    )
    op.create_index(
        "ix_tool_invocations_user_id_created_at",
        "tool_invocations",
        ["user_id", "created_at"],
        unique=False,
    )

    # Dropped children first: telemetry, credentials and claims all reference
    # devices, and Postgres will not drop a table another still points at.
    op.drop_index("ix_device_telemetry_device_id_recorded_at", table_name="device_telemetry")
    op.drop_index("ix_device_telemetry_event_type", table_name="device_telemetry")
    op.drop_index("ix_device_telemetry_device_id", table_name="device_telemetry")
    op.drop_table("device_telemetry")

    op.drop_index("ix_device_claims_provisioning_token_hash", table_name="device_claims")
    op.drop_index("ix_device_claims_code_hash", table_name="device_claims")
    op.drop_index("ix_device_claims_device_id", table_name="device_claims")
    op.drop_table("device_claims")

    op.drop_index("ix_device_credentials_device_id_active", table_name="device_credentials")
    op.drop_index("ix_device_credentials_token_hash", table_name="device_credentials")
    op.drop_index("ix_device_credentials_device_id", table_name="device_credentials")
    op.drop_table("device_credentials")

    op.drop_index("ix_devices_hardware_id", table_name="devices")
    op.drop_index("ix_devices_user_id", table_name="devices")
    op.drop_table("devices")


def downgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hardware_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("firmware_version", sa.String(length=32), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_devices_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("hardware_id", name=op.f("uq_devices_hardware_id")),
    )
    op.create_index(op.f("ix_devices_user_id"), "devices", ["user_id"], unique=False)
    op.create_index(op.f("ix_devices_hardware_id"), "devices", ["hardware_id"], unique=True)

    op.create_table(
        "device_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_credentials_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_credentials")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_device_credentials_token_hash")),
    )
    op.create_index(
        op.f("ix_device_credentials_device_id"), "device_credentials", ["device_id"], unique=False
    )
    op.create_index(
        op.f("ix_device_credentials_token_hash"),
        "device_credentials",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_device_credentials_device_id_active",
        "device_credentials",
        ["device_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "device_claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("provisioning_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_claims_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_claims")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_device_claims_code_hash")),
        sa.UniqueConstraint(
            "provisioning_token_hash", name=op.f("uq_device_claims_provisioning_token_hash")
        ),
    )
    op.create_index(
        op.f("ix_device_claims_device_id"), "device_claims", ["device_id"], unique=False
    )
    op.create_index(op.f("ix_device_claims_code_hash"), "device_claims", ["code_hash"], unique=True)
    op.create_index(
        op.f("ix_device_claims_provisioning_token_hash"),
        "device_claims",
        ["provisioning_token_hash"],
        unique=True,
    )

    op.create_table(
        "device_telemetry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("battery_percent", sa.Integer(), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("distance_cm", sa.Integer(), nullable=True),
        sa.Column("head_yaw", sa.Integer(), nullable=True),
        sa.Column("head_pitch", sa.Integer(), nullable=True),
        sa.Column("wifi_rssi", sa.Integer(), nullable=True),
        sa.Column("uptime_seconds", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "battery_percent IS NULL OR (battery_percent BETWEEN 0 AND 100)",
            name=op.f("ck_device_telemetry_battery_percent_range"),
        ),
        sa.CheckConstraint(
            "distance_cm IS NULL OR distance_cm >= 0",
            name=op.f("ck_device_telemetry_distance_non_negative"),
        ),
        sa.CheckConstraint(
            "uptime_seconds IS NULL OR uptime_seconds >= 0",
            name=op.f("ck_device_telemetry_uptime_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_telemetry_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_telemetry")),
    )
    op.create_index(
        op.f("ix_device_telemetry_device_id"), "device_telemetry", ["device_id"], unique=False
    )
    op.create_index(
        op.f("ix_device_telemetry_event_type"), "device_telemetry", ["event_type"], unique=False
    )
    op.create_index(
        "ix_device_telemetry_device_id_recorded_at",
        "device_telemetry",
        ["device_id", "recorded_at"],
        unique=False,
    )

    op.drop_index("ix_tool_invocations_user_id_created_at", table_name="tool_invocations")
    op.drop_index(op.f("ix_tool_invocations_user_id"), table_name="tool_invocations")
    op.drop_index(op.f("ix_tool_invocations_tool_name"), table_name="tool_invocations")
    op.drop_index(op.f("ix_tool_invocations_created_at"), table_name="tool_invocations")
    op.drop_table("tool_invocations")
