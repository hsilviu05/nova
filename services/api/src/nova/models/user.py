"""User account."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nova.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from nova.models.conversation import Conversation
from nova.models.device import Device
from nova.models.refresh_token import RefreshToken


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A person who owns NOVA devices, conversations, and memories."""

    __tablename__ = "users"
    __table_args__ = (
        # The API lower-cases before writing; enforcing it here keeps the
        # unique index meaningful against direct SQL writes too.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    # Stored lower-cased and unique; the API normalises before writing so a
    # citext extension is not needed.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    devices: Mapped[list[Device]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} email={self.email!r}>"
