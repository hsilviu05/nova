"""Dependency probes behind ``/health`` and ``/ready``."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nova.core.logging import get_logger
from nova.schemas.health import DependencyStatus, ReadinessResponse

logger = get_logger(__name__)


class HealthService:
    """Probes the dependencies the API cannot serve traffic without."""

    def __init__(self, *, engine: AsyncEngine, redis: Redis) -> None:
        self._engine = engine
        self._redis = redis

    async def check_readiness(self) -> ReadinessResponse:
        """Probe every dependency and report a combined verdict."""
        dependencies = [
            await self._probe("postgres", self._ping_postgres),
            await self._probe("redis", self._ping_redis),
        ]
        return ReadinessResponse(
            ready=all(d.healthy for d in dependencies),
            dependencies=dependencies,
        )

    async def _probe(self, name: str, probe: Callable[[], Awaitable[None]]) -> DependencyStatus:
        started = time.perf_counter()
        try:
            await probe()
        except Exception as exc:
            logger.warning("dependency_unhealthy", dependency=name, error=str(exc))
            return DependencyStatus(name=name, healthy=False, error=type(exc).__name__)

        elapsed_ms = (time.perf_counter() - started) * 1000
        return DependencyStatus(name=name, healthy=True, latency_ms=round(elapsed_ms, 2))

    async def _ping_postgres(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def _ping_redis(self) -> None:
        await self._redis.ping()
