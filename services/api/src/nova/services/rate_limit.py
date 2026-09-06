"""Redis-backed fixed-window rate limiting.

Fixed window rather than a sliding log: one INCR plus one conditional EXPIRE
per request, no per-request member set to trim. The trade-off is a burst of
up to 2x the limit across a window boundary, which is acceptable for slowing
credential stuffing.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from nova.core.errors import RateLimitError
from nova.core.logging import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """Counts events per key inside a fixed window."""

    def __init__(self, redis: Redis, *, namespace: str = "ratelimit") -> None:
        self._redis = redis
        self._namespace = namespace

    async def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        """Record one hit against ``key``.

        Raises:
            RateLimitError: once ``limit`` hits occur inside the window.

        A Redis outage does not block authentication: the limiter fails open
        and logs, because losing the cache should degrade abuse protection,
        not take login down.
        """
        redis_key = f"{self._namespace}:{key}"
        try:
            pipe = self._redis.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            count, ttl = await pipe.execute()

            if ttl < 0:
                # First hit in this window (or a key with no expiry set).
                await self._redis.expire(redis_key, window_seconds)
                ttl = window_seconds
        except RedisError as exc:
            logger.warning("rate_limiter_unavailable", error=str(exc))
            return

        if int(count) > limit:
            logger.info("rate_limit_exceeded", key=redis_key, count=int(count))
            raise RateLimitError(retry_after_seconds=max(int(ttl), 1))
