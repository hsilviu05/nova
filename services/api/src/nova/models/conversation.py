"""Conversations and the messages in them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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

MessageRole = Literal["user", "assistant"]

# Only these two are stored. The system prompt is rendered per request from
# the persona rather than persisted, so tuning NOVA's character changes every
# future turn instead of only new conversations.
ROLES: tuple[str, ...] = ("user", "assistant")


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A thread of messages between a user and NOVA."""

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Derived from the opening message rather than asked for. Nullable until
    # that first message exists.
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Denormalised so the conversation list can sort by recency without
    # joining every message row.
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    owner: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at",
    )

    __table_args__ = (
        # The conversation list is always "mine, most recent first".
        Index("ix_conversations_user_id_last_message_at", "user_id", "last_message_at"),
        CheckConstraint("message_count >= 0", name="message_count_non_negative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation id={self.id} user_id={self.user_id}>"


class Message(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One turn.

    Assistant messages record which model produced them and what it cost, so
    a reply can be traced to a provider and a bill later. Those columns are
    null on user messages.
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Wall-clock time to produce the reply, which is what the analytics
    # screen reports as "AI response latency".
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        # Replaying a conversation is always "this thread, oldest first".
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
        CheckConstraint("role IN ('user', 'assistant')", name="role_known"),
        CheckConstraint("length(content) > 0", name="content_not_empty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message id={self.id} role={self.role!r}>"
