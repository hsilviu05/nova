"""Rate limiting on the unauthenticated auth endpoints."""

from __future__ import annotations

import pytest
from redis.asyncio import Redis

from nova.core.errors import RateLimitError
from nova.services.rate_limit import RateLimiter

pytestmark = pytest.mark.integration


class TestRateLimiter:
    async def test_allows_up_to_the_limit(self, redis_client: Redis) -> None:
        limiter = RateLimiter(redis_client)
        for _ in range(3):
            await limiter.check("scope:1.2.3.4", limit=3, window_seconds=60)

    async def test_raises_past_the_limit(self, redis_client: Redis) -> None:
        limiter = RateLimiter(redis_client)
        for _ in range(3):
            await limiter.check("scope:1.2.3.4", limit=3, window_seconds=60)

        with pytest.raises(RateLimitError) as exc:
            await limiter.check("scope:1.2.3.4", limit=3, window_seconds=60)
        assert exc.value.retry_after_seconds > 0

    async def test_keys_are_independent(self, redis_client: Redis) -> None:
        limiter = RateLimiter(redis_client)
        await limiter.check("scope:1.1.1.1", limit=1, window_seconds=60)
        # A different caller must not inherit the first one's count.
        await limiter.check("scope:2.2.2.2", limit=1, window_seconds=60)

    async def test_sets_an_expiry_on_the_counter(self, redis_client: Redis) -> None:
        limiter = RateLimiter(redis_client)
        await limiter.check("scope:1.2.3.4", limit=5, window_seconds=60)
        # Without a TTL the counter would never reset and the caller would be
        # locked out permanently.
        assert await redis_client.ttl("ratelimit:scope:1.2.3.4") > 0

    async def test_fails_open_when_redis_is_down(self, unreachable_redis: Redis) -> None:
        """Losing Redis must degrade abuse protection, not take login down.

        ``aclose()`` on a live client would not test this -- redis-py simply
        reconnects on the next command. This client points at a closed port.
        """
        limiter = RateLimiter(unreachable_redis)
        for _ in range(5):
            await limiter.check("scope:1.2.3.4", limit=1, window_seconds=60)


class TestLoginRateLimit:
    """The limiter as the login endpoint actually applies it.

    These use ``throttled_client`` rather than mutating the session-scoped
    settings, which would leak a 3-attempt limit into every later test.
    """

    async def test_repeated_failures_are_throttled(self, throttled_client, credentials) -> None:
        payload = {"email": credentials["email"], "password": "wrong-password"}
        statuses = [
            (await throttled_client.post("/api/v1/auth/login", json=payload)).status_code
            for _ in range(5)
        ]

        assert statuses[:3] == [401, 401, 401]
        assert statuses[3:] == [429, 429]

    async def test_429_advertises_retry_after(self, throttled_client, credentials) -> None:
        payload = {"email": credentials["email"], "password": "wrong-password"}
        for _ in range(3):
            await throttled_client.post("/api/v1/auth/login", json=payload)

        response = await throttled_client.post("/api/v1/auth/login", json=payload)

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        assert response.json()["error"]["code"] == "rate_limited"

    async def test_a_successful_login_still_counts(self, throttled_client, credentials) -> None:
        """The limit is per caller, not per failure.

        Counting only failures would let an attacker with a valid account
        reset the window at will.
        """
        await throttled_client.post("/api/v1/auth/register", json=credentials)

        payload = {"email": credentials["email"], "password": credentials["password"]}
        statuses = [
            (await throttled_client.post("/api/v1/auth/login", json=payload)).status_code
            for _ in range(4)
        ]
        assert statuses[-1] == 429
