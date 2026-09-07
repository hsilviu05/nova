"""Async engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nova.core.config import DatabaseSettings


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build the async engine for ``settings``."""
    return create_async_engine(
        settings.dsn(),
        echo=settings.echo,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # Recycle before common 1-hour idle timeouts on managed Postgres.
        pool_recycle=settings.pool_recycle_seconds,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session that commits on success and rolls back on failure.

    One transaction per request: handlers never call ``commit`` themselves,
    so a handler that raises halfway through cannot leave a partial write.

    A context manager rather than a bare async generator so that callers
    close it with ``async with``. A caller that drives it by hand -- or
    delegates to it with ``async for`` -- leaves it suspended when an
    exception unwinds the caller, and the session is then closed whenever
    the garbage collector happens to reach the generator, which asyncio runs
    as a detached task: a ROLLBACK arriving on a pooled connection that some
    later request already owns.
    """
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
