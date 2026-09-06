"""Application factory and process lifecycle.

Dependencies are opened once on startup and closed on shutdown. Nothing is
constructed at import time, so tests can build an app against their own
settings without the module having already connected to something.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nova import __version__
from nova.ai.registry import build_chat_provider, build_embedding_provider
from nova.api.v1.health import router as health_router
from nova.api.v1.router import router as v1_router
from nova.core.config import Settings, get_settings
from nova.core.logging import configure_logging, get_logger
from nova.core.security import Argon2PasswordHasher, TokenService
from nova.db.redis import create_redis
from nova.db.session import create_engine, create_session_factory
from nova.middleware.errors import register_exception_handlers
from nova.middleware.request_context import RequestContextMiddleware
from nova.services.connections import InMemoryConnectionRegistry

logger = get_logger(__name__)

DESCRIPTION = """
NOVA is a physical AI companion and behavioral intelligence platform.

This API backs the mobile app and the ESP32-S3 device: accounts, devices,
conversations, semantic memory, telemetry, analytics, and predictions.
"""


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Create the lifespan handler bound to ``settings``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database)
        redis = create_redis(settings.redis)

        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        app.state.token_service = TokenService(settings.jwt)
        app.state.password_hasher = Argon2PasswordHasher(settings.security)
        # Device sockets are held in this process. Scaling horizontally means
        # replacing this with a Redis-backed registry -- see ADR 004.
        app.state.connections = InMemoryConnectionRegistry()
        # One provider for the process: it holds an HTTP client and
        # connection pool that should not be rebuilt per request.
        app.state.chat_provider = build_chat_provider(settings.ai)
        # Built at startup so a width that disagrees with the memories column
        # fails here rather than on the first message someone sends.
        app.state.embedding_provider = build_embedding_provider(settings.ai)

        logger.info("api_started", version=__version__, environment=settings.environment)
        try:
            yield
        finally:
            # Close in reverse order of acquisition; both must run even if one
            # raises, or the process can hang on shutdown.
            try:
                await redis.aclose()
            finally:
                await engine.dispose()
            logger.info("api_stopped")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or get_settings()
    configure_logging(settings.observability)

    app = FastAPI(
        title="NOVA API",
        description=DESCRIPTION,
        version=__version__,
        docs_url=settings.docs_url,
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=_build_lifespan(settings),
    )
    app.state.settings = settings

    # Order matters: CORS is added last so it runs first and can answer a
    # preflight before anything else touches the request.
    app.add_middleware(RequestContextMiddleware)
    if settings.security.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.security.cors_origins,
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )

    register_exception_handlers(app)

    # Probes live outside the versioned prefix: orchestrators should not have
    # to track the API version to know whether a container is alive.
    app.include_router(health_router)
    app.include_router(v1_router, prefix=settings.api_v1_prefix)

    return app
