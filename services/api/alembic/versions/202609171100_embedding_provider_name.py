"""Widen memories.embedding_provider to carry a model name.

The provider name on each memory is what keeps vectors from different
embedders apart at retrieval. "openai_compatible" alone was not enough:
two models behind the same endpoint produce incomparable vectors, so the
name now carries the model too, and 32 characters no longer fit it.

Revision ID: c9e1f3a5b7d2
Revises: b7d2e4f6a8c0
Create Date: 2026-09-17 11:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e1f3a5b7d2"
down_revision: str | None = "b7d2e4f6a8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "memories",
        "embedding_provider",
        existing_type=sa.String(length=32),
        type_=sa.String(length=96),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Truncating a name that no longer fits would corrupt it silently; a row
    # written under a long name has to be re-embedded before going back.
    op.execute("UPDATE memories SET embedding_provider = left(embedding_provider, 32)")
    op.alter_column(
        "memories",
        "embedding_provider",
        existing_type=sa.String(length=96),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
