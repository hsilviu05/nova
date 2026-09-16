"""The audit log.

Every tool NOVA runs leaves a row here -- the ones a person pressed, the ones
the model asked for, the ones that were refused, and the ones that failed.
That completeness is the point. A log of successes answers "what happened";
a log that includes refusals answers "what was attempted", which is the
question that matters after something goes wrong.

This table also does the job device telemetry used to: it is what the
dashboard's recent-activity list reads, and what "what did NOVA do today"
is answered from.

Append-only, so there is no ``updated_at``. Arguments are stored, results
are not: a result can be megabytes of log output, and the row exists to
record that something ran, not to be a second copy of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nova.core.clock import utc_now
from nova.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nova.models.user import User

# What a completed invocation can have ended as. A plain string rather than a
# database enum: the set will grow, and a native enum makes each addition a
# migration.
STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",  # the tool ran and could not do what was asked
    "refused",  # the gate said no: permission, unknown tool, bad input
    "timed_out",
)


class ToolInvocation(Base, UUIDPrimaryKeyMixin):
    """One attempt to run a tool."""

    __tablename__ = "tool_invocations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SET NULL rather than CASCADE: deleting a conversation should not erase
    # the record of what was run while it was open.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool_group: Mapped[str] = mapped_column(String(24), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Machine-readable reason on anything that is not "succeeded".
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # True when the model asked for this rather than a person pressing a
    # button. The distinction is the first thing anyone reviewing this log
    # wants to know.
    initiated_by_model: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True when a person explicitly approved this specific call.
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Redacted before storage by the service -- arguments are a place
    # credentials end up, particularly for the shell tool.
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Correlates a row with the server logs for the request that caused it.
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )

    owner: Mapped[User] = relationship(back_populates="tool_invocations")

    __table_args__ = (
        # The activity feed is always "mine, most recent first".
        Index("ix_tool_invocations_user_id_created_at", "user_id", "created_at"),
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'refused', 'timed_out')",
            name="status_known",
        ),
        CheckConstraint("permission IN ('read', 'write', 'destructive')", name="permission_known"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ToolInvocation {self.tool_name!r} {self.status!r}>"
