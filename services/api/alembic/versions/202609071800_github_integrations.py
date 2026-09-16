"""github integrations

Revision ID: 8c1f2a7d4e90
Revises: d5949ac01523
Create Date: 2026-09-07 18:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c1f2a7d4e90"
down_revision: str | None = "d5949ac01523"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("webhook_secret", sa.String(length=128), nullable=False),
        sa.Column("repository", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event", sa.String(length=64), nullable=True),
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
            ["user_id"],
            ["users.id"],
            name=op.f("fk_github_integrations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_integrations")),
    )
    op.create_index(
        op.f("ix_github_integrations_user_id"), "github_integrations", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_github_integrations_user_id"), table_name="github_integrations")
    op.drop_table("github_integrations")
