"""Redis client lifecycle."""

from __future__ import annotations

from redis.asyncio import Redis

from nova.core.config import RedisSettings


def create_redis(settings: RedisSettings) -> Redis:
    """Build an async Redis client.

    ``decode_responses`` keeps call sites working in ``str`` rather than
    sprinkling ``.decode()`` through the rate limiter and cache code.
    """
    client: Redis = Redis.from_url(
        settings.dsn(),
        decode_responses=True,
        socket_timeout=settings.socket_timeout_seconds,
        socket_connect_timeout=settings.socket_timeout_seconds,
        health_check_interval=30,
    )
    return client
