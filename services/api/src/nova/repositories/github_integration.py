"""Persistence for GitHub integrations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from nova.models.github_integration import GitHubIntegration
from nova.repositories.base import BaseRepository


class GitHubIntegrationRepository(BaseRepository):
    async def get(self, integration_id: uuid.UUID) -> GitHubIntegration | None:
        return await self._session.get(GitHubIntegration, integration_id)

    async def get_for_user(self, user_id: uuid.UUID) -> GitHubIntegration | None:
        stmt = select(GitHubIntegration).where(GitHubIntegration.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, integration: GitHubIntegration) -> GitHubIntegration:
        self._session.add(integration)
        await self._session.flush()
        return integration

    async def delete_for_user(self, user_id: uuid.UUID) -> bool:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(GitHubIntegration).where(GitHubIntegration.user_id == user_id)
            ),
        )
        return bool(result.rowcount)

    async def record_delivery(
        self, integration: GitHubIntegration, *, at: datetime, event: str
    ) -> None:
        integration.last_delivery_at = at
        integration.last_event = event
        await self._session.flush()
