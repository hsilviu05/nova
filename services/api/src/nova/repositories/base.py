"""Repository base.

Repositories own SQL. Services own decisions. Routes own neither -- that
separation is what keeps queries out of handlers and business rules out of
the ORM layer.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Holds the unit-of-work session shared by a request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session
