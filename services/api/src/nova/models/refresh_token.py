"""Refresh token records.

Rows store only a SHA-256 digest of the token. ``family_id`` groups every
token descended from one login, which is what makes reuse detection possible:
replaying a rotated token revokes the whole family rather than just itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nova.core.clock import utc_now
from nova.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nova.models.user import User


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One issued refresh token."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # Partial index backing "revoke everything still live for this user".
        # Excluding revoked rows keeps it small as token history accumulates.
        Index(
            "ix_refresh_tokens_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 hex digest: 64 characters, unique so a replay maps to exactly
    # one row.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # All tokens rotated from a single login share this identifier.
    family_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Audit context. Nullable: a client behind a proxy may supply neither.
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= utc_now()

    @property
    def is_usable(self) -> bool:
        """True only for a live, unrotated token."""
        return not self.is_revoked and not self.is_expired

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RefreshToken id={self.id} user_id={self.user_id} revoked={self.is_revoked}>"
