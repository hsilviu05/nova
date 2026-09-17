"""Alert persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, desc, func, select, update

from nova.models.alert import Alert
from nova.repositories.base import BaseRepository


class AlertRepository(BaseRepository):
    def add(self, alert: Alert) -> Alert:
        self._session.add(alert)
        return alert

    async def get(self, alert_id: uuid.UUID) -> Alert | None:
        return await self._session.get(Alert, alert_id)

    async def list_recent(
        self, *, limit: int = 50, unacknowledged_only: bool = False
    ) -> list[Alert]:
        """Newest first."""
        stmt = select(Alert)
        if unacknowledged_only:
            stmt = stmt.where(Alert.acknowledged_at.is_(None))
        stmt = stmt.order_by(desc(Alert.created_at), desc(Alert.id)).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_unacknowledged(self) -> int:
        stmt = select(func.count()).select_from(Alert).where(Alert.acknowledged_at.is_(None))
        return int((await self._session.execute(stmt)).scalar_one())

    async def latest_kind_by_project(self) -> dict[str, str]:
        """The most recent alert kind for each project.

        What the watcher restores its idea of "currently down" from after a
        restart, so a project that went down yesterday is not announced
        again this morning.
        """
        stmt = (
            select(Alert.project, Alert.kind)
            .distinct(Alert.project)
            .order_by(Alert.project, desc(Alert.created_at), desc(Alert.id))
        )
        return {str(row[0]): str(row[1]) for row in (await self._session.execute(stmt)).all()}

    async def acknowledge(self, alert_id: uuid.UUID, *, at: datetime) -> Alert | None:
        alert = await self.get(alert_id)
        if alert is None:
            return None
        if alert.acknowledged_at is None:
            alert.acknowledged_at = at
            await self._session.flush()
        return alert

    async def acknowledge_all(self, *, at: datetime) -> int:
        stmt = update(Alert).where(Alert.acknowledged_at.is_(None)).values(acknowledged_at=at)
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return int(result.rowcount or 0)
