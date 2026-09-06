"""Long-term memory.

What NOVA believes about its owner, extracted from conversation and retrieved
by similarity before it answers.

Every row is visible, editable, and deletable by the person it is about. That
is not a feature bolted on for a settings screen -- a device that forms
opinions about someone in a room with them has to let them read and correct
those opinions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nova.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nova.models.user import User

# The vector width is baked into the column, so this is schema, not
# configuration. Changing it needs a migration *and* re-embedding every
# existing row -- vectors of different widths cannot be compared at all.
EMBEDDING_DIMENSIONS = 1536

# A small, closed taxonomy. Deliberately not open-ended: a category set that
# grows with every extraction stops being useful for filtering, and the point
# of categories is that a person can scan them.
CATEGORIES: tuple[str, ...] = (
    "preference",  # likes, dislikes, how they want things done
    "fact",  # stable truths about them or their world
    "routine",  # recurring patterns in time
    "relationship",  # people and animals in their life
    "project",  # what they are working on
    "event",  # something that happened, with a date attached
)


class Memory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One thing NOVA remembers."""

    __tablename__ = "memories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)

    # How much this should influence a reply, 0-1. Used to rank retrieval and
    # to decide what survives a prune.
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # How sure the extractor was that this is true, 0-1. Kept separate from
    # importance: "they mentioned a wife once" can be highly important and
    # barely confirmed.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Which provider produced the vector. Vectors from different embedders
    # are not comparable, so a provider change has to be *detectable* rather
    # than silently degrading every future search.
    embedding_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="lexical")

    # Where this came from, so the owner can see why NOVA believes it.
    # SET NULL rather than CASCADE: deleting a conversation should not erase
    # what NOVA learned from it.
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    recall_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[User] = relationship(back_populates="memories")

    __table_args__ = (
        Index("ix_memories_user_id_category", "user_id", "category"),
        Index("ix_memories_user_id_importance", "user_id", "importance"),
        # Approximate nearest-neighbour over cosine distance.
        #
        # Worth stating plainly: every NOVA search also filters by user_id,
        # and HNSW applies that filter *after* traversing the graph, so at
        # small per-user counts the planner will often prefer -- and should
        # prefer -- an exact scan, which is faster and perfectly recalled.
        # This index earns its place once one account holds tens of thousands
        # of memories; it is declared now so that transition needs no
        # migration.
        Index(
            "ix_memories_embedding_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_in_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
        CheckConstraint("length(content) > 0", name="content_not_empty"),
        CheckConstraint(
            "category IN ('preference', 'fact', 'routine', 'relationship', 'project', 'event')",
            name="category_known",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Memory id={self.id} category={self.category!r}>"
