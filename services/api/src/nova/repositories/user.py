"""User persistence."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from nova.models.user import User
from nova.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.user.User` rows."""

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Look up by normalised email."""
        stmt = select(User).where(User.email == email.strip().lower())
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def exists_by_email(self, email: str) -> bool:
        stmt = select(User.id).where(User.email == email.strip().lower()).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    def add(self, user: User) -> User:
        """Stage ``user`` for insertion. The request transaction commits it."""
        self._session.add(user)
        return user
