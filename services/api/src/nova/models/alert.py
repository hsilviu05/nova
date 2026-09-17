"""Alerts: a project changing state while nobody was looking.

The watcher writes one row when a configured project goes down and one
when it comes back. The rows are what the dashboard shows and what the
phone notifies about, and they stay until acknowledged, so a failure at
3 a.m. is still on the screen at 9.

Server-wide rather than per user: projects are server configuration, and
whoever is signed in to this NOVA is who the alert is for.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nova.core.clock import utc_now
from nova.db.base import Base, UUIDPrimaryKeyMixin

KINDS: tuple[str, ...] = ("project_down", "project_recovered")


class Alert(Base, UUIDPrimaryKeyMixin):
    """One change of state for one project."""

    __tablename__ = "alerts"

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    project: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The feed is newest first; the badge is "how many not yet seen".
        Index("ix_alerts_created_at", "created_at"),
        Index("ix_alerts_acknowledged_at", "acknowledged_at"),
        CheckConstraint("kind IN ('project_down', 'project_recovered')", name="kind_known"),
    )

    @property
    def acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Alert {self.kind} {self.project}>"
