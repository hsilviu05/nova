"""Refresh-token persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update

from nova.models.refresh_token import RefreshToken
from nova.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.refresh_token.RefreshToken` rows."""

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        return token

    async def revoke_family(self, family_id: uuid.UUID, *, at: datetime) -> int:
        """Revoke every live token in ``family_id``.

        Used both on logout and on reuse detection. Returns the number of
        tokens revoked, which the caller logs as a security signal.
        """
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, at: datetime) -> int:
        """Revoke every live token belonging to ``user_id``."""
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return int(result.rowcount or 0)
