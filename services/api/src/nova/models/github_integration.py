"""A user's GitHub webhook integration.

One per user. The secret is what GitHub signs deliveries with, so it has to
be stored as it is -- HMAC verification needs the shared key, not a hash of
it. It is generated here, shown to the user exactly once at creation, and
never returned again; rotating it is the only way to see a new one. That is
the same trade the device token in the firmware's NVS makes, and the same
mitigation applies: it is revocable in one call.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from nova.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GitHubIntegration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "github_integrations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    # Shared with GitHub; verifies every delivery. See the module docstring.
    webhook_secret: Mapped[str] = mapped_column(String(128), nullable=False)
    # "owner/name". When set, deliveries from any other repository are
    # acknowledged and ignored -- a webhook is per-repository on GitHub's
    # side, but an organisation-level hook can carry many.
    repository: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # e.g. "workflow_run:success". For the settings screen, so "is this
    # wired up?" has an answer without reading server logs.
    last_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
