"""Shared test fixtures.

Integration tests run against a real PostgreSQL and Redis rather than
in-memory fakes: the things most likely to break -- the unique index behind
reuse detection, ``ON DELETE CASCADE``, partial indexes, INET columns -- do
not exist in SQLite, so a SQLite suite would pass while production fails.

Point the suite at a database with NOVA_TEST_DATABASE__*; it defaults to a
``nova_test`` database on localhost. The schema is created once per session
and every test runs inside a transaction that is rolled back afterwards, so
tests neither see nor leave each other's rows.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from nova.ai.base import ChatProvider, EmbeddingProvider
from nova.ai.registry import build_chat_provider, build_embedding_provider
from nova.core.config import (
    DatabaseSettings,
    JWTSettings,
    ObservabilitySettings,
    RedisSettings,
    SecuritySettings,
    Settings,
)
from nova.core.security import Argon2PasswordHasher, TokenService
from nova.db.base import Base
from nova.db.redis import create_redis
from nova.main import create_app
from nova.services.connections import InMemoryConnectionRegistry

# Long enough to satisfy the 32-character minimum; test-only, never deployed.
TEST_JWT_SECRET = "test-secret-key-for-nova-suite-do-not-use-in-production"


def _test_database_settings() -> DatabaseSettings:
    return DatabaseSettings(
        host=os.getenv("NOVA_TEST_DATABASE__HOST", "127.0.0.1"),
        port=int(os.getenv("NOVA_TEST_DATABASE__PORT", "5432")),
        user=os.getenv("NOVA_TEST_DATABASE__USER", "nova"),
        password=os.getenv("NOVA_TEST_DATABASE__PASSWORD", "nova_dev_password"),  # type: ignore[arg-type]
        name=os.getenv("NOVA_TEST_DATABASE__NAME", "nova_test"),
    )


def build_test_app(
    settings: Settings,
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    connections: InMemoryConnectionRegistry | None = None,
    chat_provider: ChatProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> FastAPI:
    """Build an app wired to test fixtures instead of its own lifespan.

    Every client fixture goes through here. When the application grows a new
    piece of ``app.state``, it is added once rather than in each fixture --
    which is how a missing ``connections`` registry first showed up as three
    identical failures.
    """
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.token_service = TokenService(settings.jwt)
    app.state.password_hasher = Argon2PasswordHasher(settings.security)
    app.state.connections = connections or InMemoryConnectionRegistry()
    # Offline by default, so the suite exercises the whole conversation path
    # with no API key, no network, and no per-run cost.
    app.state.chat_provider = chat_provider or build_chat_provider(settings.ai)
    app.state.embedding_provider = embedding_provider or build_embedding_provider(settings.ai)
    return app


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Test settings.

    Argon2 work factors are dropped to the library minimum: the suite creates
    many users, and production factors would add minutes of pure KDF time
    without testing anything the low factors do not.
    """
    return Settings(
        environment="test",
        database=_test_database_settings(),
        redis=RedisSettings(
            host=os.getenv("NOVA_TEST_REDIS__HOST", "127.0.0.1"),
            port=int(os.getenv("NOVA_TEST_REDIS__PORT", "6379")),
            db=int(os.getenv("NOVA_TEST_REDIS__DB", "15")),
        ),
        jwt=JWTSettings(secret_key=TEST_JWT_SECRET),  # type: ignore[arg-type]
        security=SecuritySettings(
            argon2_time_cost=1,
            argon2_memory_cost_kib=8,
            argon2_parallelism=1,
            # High enough that ordinary tests never trip it; the rate-limit
            # tests set their own limits explicitly.
            auth_rate_limit_attempts=1000,
        ),
        observability=ObservabilitySettings(log_level="WARNING", log_json=True),
    )


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Session-scoped engine with the schema created once.

    ``NullPool``, unlike production, and this is the one thing about the test
    engine that is load-bearing rather than incidental.

    pytest-asyncio runs async fixture setup and teardown in *different tasks*
    -- the WebSocket tests below already have to work around it. So the
    ``connection`` fixture's teardown (roll back, hand the connection back)
    is not guaranteed to have finished when the next test's setup checks one
    out. With a pool, the next test can be given a connection whose previous
    owner is still releasing it, and asyncpg says so in two ways that look
    like unrelated bugs:

        cannot perform operation: another operation is in progress
        cannot use Connection.transaction() in a manually started transaction

    That surfaced once in CI as two errors in the device tests and did not
    reproduce locally in a hundred runs, because it needs a loaded machine to
    widen the window. NullPool removes the coupling instead of narrowing it:
    every test opens its own connection and closes it, so a slow teardown can
    delay the next test but cannot corrupt it.

    The cost is measured, not assumed: 455 tests go from ~25.7s to ~38s
    locally, about 27ms per test for connecting and authenticating. That is
    a real price and it is worth paying. A pool that hands out a connection
    another task has not finished with does not only produce errors -- it can
    just as easily produce a test that passes against someone else's
    transaction, and a suite that lies occasionally is worth less than a
    suite that is twelve seconds slower.
    """
    # Built here rather than via create_engine(): that shapes a pool for
    # production (size, overflow, recycle, pre-ping), none of which applies
    # when there is no pool.
    engine = create_async_engine(settings.database.dsn(), poolclass=NullPool)
    async with engine.begin() as conn:
        # The migrations enable this; ``create_all`` does not, and the
        # memories table cannot be built without the vector type.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """An open transaction that is rolled back when the test ends."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
def session_factory(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions bound to the test's transaction.

    ``join_transaction_mode="create_savepoint"`` lets application code call
    ``commit()`` normally -- it releases a savepoint rather than committing
    the outer transaction, which the fixture still rolls back.
    """
    return async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncIterator[Redis]:
    """A Redis client on a dedicated test database, flushed around each test."""
    client = create_redis(settings.redis)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
async def client(
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app, sharing the test's transaction.

    The app is built without its lifespan so that ``app.state`` points at the
    fixtures above instead of opening its own connections.
    """
    app = build_test_app(
        settings, engine=engine, session_factory=session_factory, redis=redis_client
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://nova.test") as http_client:
        yield http_client


@pytest.fixture
def credentials() -> dict[str, str]:
    """A unique registration payload."""
    return {
        "email": f"user-{uuid.uuid4().hex[:12]}@example.com",
        "password": "correct-horse-battery-staple",
        "display_name": "Test User",
    }


@pytest.fixture
async def registered(client: AsyncClient, credentials: dict[str, str]) -> dict[str, object]:
    """A registered user plus their initial token pair."""
    response = await client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def auth_headers(registered: dict[str, object]) -> dict[str, str]:
    tokens = registered["tokens"]
    assert isinstance(tokens, dict)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
async def throttled_client(
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[AsyncClient]:
    """A client whose app allows only three auth attempts per window.

    Built from a copy of the settings so the low limit cannot leak into any
    other test through the session-scoped fixture.
    """
    throttled = settings.model_copy(deep=True)
    throttled.security.auth_rate_limit_attempts = 3
    throttled.security.auth_rate_limit_window_seconds = 60

    app = build_test_app(
        throttled, engine=engine, session_factory=session_factory, redis=redis_client
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://nova.test"
    ) as http_client:
        yield http_client


@pytest.fixture
async def unreachable_redis() -> AsyncIterator[Redis]:
    """A Redis client that genuinely cannot connect.

    Calling ``aclose()`` on a live client does not simulate an outage --
    redis-py transparently reconnects on the next command. Pointing at a
    closed port does, and with a short timeout it fails fast.
    """
    client = create_redis(RedisSettings(host="127.0.0.1", port=1, socket_timeout_seconds=0.25))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def degraded_client(
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    unreachable_redis: Redis,
) -> AsyncIterator[AsyncClient]:
    """A client whose app has a working database but no reachable Redis."""
    app = build_test_app(
        settings,
        engine=engine,
        session_factory=session_factory,
        redis=unreachable_redis,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://nova.test"
    ) as http_client:
        yield http_client
