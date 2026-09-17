"""Conversation search: trigram indexes on titles and message content.

Searching a thread is a substring match -- "postg" should find "PostgreSQL",
which stemmed full-text search would not do -- so pg_trgm rather than
tsvector. The extension ships with every Postgres build this project runs
on, including the pgvector image; ``IF NOT EXISTS`` because a shared
database may already have it.

Revision ID: b7d2e4f6a8c0
Revises: 4f0c1ab7d9e2
Create Date: 2026-09-17 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b7d2e4f6a8c0"
down_revision: str | None = "4f0c1ab7d9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_messages_content_trgm",
        "messages",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_conversations_title_trgm",
        "conversations",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )


def downgrade() -> None:
    # The extension stays: dropping it would also drop any other trigram
    # index in the database, and it costs nothing when unused.
    op.drop_index("ix_conversations_title_trgm", table_name="conversations")
    op.drop_index("ix_messages_content_trgm", table_name="messages")
