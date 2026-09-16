"""Audit-log persistence and the counts the dashboard reads."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import desc, func, select

from nova.models.tool_invocation import ToolInvocation
from nova.repositories.base import BaseRepository


class ToolInvocationRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.tool_invocation.ToolInvocation` rows."""

    def add(self, invocation: ToolInvocation) -> ToolInvocation:
        self._session.add(invocation)
        return invocation

    async def list_for_owner(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
        tool_name: str | None = None,
    ) -> list[ToolInvocation]:
        """Most recent first, which is the only order an activity feed wants."""
        stmt = select(ToolInvocation).where(ToolInvocation.user_id == owner_id)
        if tool_name is not None:
            stmt = stmt.where(ToolInvocation.tool_name == tool_name)

        stmt = stmt.order_by(desc(ToolInvocation.created_at)).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_since(self, owner_id: uuid.UUID, *, since: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(ToolInvocation)
            .where(ToolInvocation.user_id == owner_id, ToolInvocation.created_at >= since)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_failures_since(self, owner_id: uuid.UUID, *, since: datetime) -> int:
        """Anything that did not succeed, which is what the dashboard flags.

        Refusals count. A refused call is not a fault, but a run of them is
        worth seeing -- it is either a misconfiguration or something probing
        at the gate.
        """
        stmt = (
            select(func.count())
            .select_from(ToolInvocation)
            .where(
                ToolInvocation.user_id == owner_id,
                ToolInvocation.created_at >= since,
                ToolInvocation.status != "succeeded",
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())
