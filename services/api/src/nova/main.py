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
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from nova.middleware.hardening import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from nova.middleware.request_context import RequestContextMiddleware
from nova.repositories.memory import MemoryRepository
from nova.tools.registry import KnowledgeDependencies, build_registry

logger = get_logger(__name__)

DESCRIPTION = """
NOVA is a local-first personal AI terminal.

This API is the whole of NOVA apart from its screen: accounts, conversations,
streaming replies, semantic memory, a permissioned tool system for inspecting
the machine and the services on it, and the audit log of everything it did.

The iPhone app is a client. Nothing here assumes one is connected.
"""


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Create the lifespan handler bound to ``settings``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database)
        redis = create_redis(settings.redis)
        session_factory = create_session_factory(engine)

        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis
        app.state.token_service = TokenService(settings.jwt)
        app.state.password_hasher = Argon2PasswordHasher(settings.security)
        # One provider for the process: it holds an HTTP client and
        # connection pool that should not be rebuilt per request.
        chat_provider = build_chat_provider(settings.ai)
        # Built at startup so a width that disagrees with the memories column
        # fails here rather than on the first message someone sends.
        embedding_provider = build_embedding_provider(settings.ai)
        app.state.chat_provider = chat_provider
        app.state.embedding_provider = embedding_provider

        # Built once, from configuration, before any request arrives. What
        # NOVA can do to this machine is decided here and nowhere else.
        app.state.tool_registry = build_registry(
            tools=settings.tools,
            integrations=settings.integrations,
            knowledge=KnowledgeDependencies(
                session_factory=session_factory,
                embeddings=embedding_provider,
                ai=settings.ai,
            ),
        )

        # A changed embedder leaves the previous rows invisible to
        # retrieval, which is the safe failure but a quiet one. Say so at
        # the one moment an operator is looking: startup. It is the only
        # thing startup asks the database, and it must stay advisory: the
        # process has to come up with no database at all, because that is
        # how the container is smoke-tested and how /health stays a probe of
        # the process rather than of its dependencies.
        try:
            async with session_factory() as session:
                stale = await MemoryRepository(session).count_stale(embedding_provider.name)
        except Exception as exc:  # any failure here is advisory
            logger.warning("stale_memory_check_skipped", reason=type(exc).__name__)
        else:
            if stale:
                logger.warning(
                    "memories_need_reembedding",
                    stale=stale,
                    embedding_provider=embedding_provider.name,
                    fix="python scripts/reembed_memories.py",
                )

        logger.info(
            "api_started",
            version=__version__,
            environment=settings.environment,
            chat_provider=chat_provider.name,
            embedding_provider=embedding_provider.name,
            tools=len(app.state.tool_registry),
        )
        try:
            yield
        finally:
            # Every acquisition is released, and each in its own try so one
            # failure cannot skip the rest and hang the process on shutdown.
            try:
                await chat_provider.aclose()
            finally:
                try:
                    await embedding_provider.aclose()
                finally:
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

    # Order matters: middleware added later runs earlier. The body cap goes
    # innermost so that its refusal is raised from inside the endpoint's own
    # read of the body, where the exception handlers turn it into the
    # standard 413 envelope. One layer further out and Starlette's
    # BaseHTTPMiddleware would re-raise it from its streaming task instead.
    # Then the request id, the security headers on the way out, the Host
    # check, and CORS outermost so it can answer a preflight before anything
    # else touches the request.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.security.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.security.allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.security.allowed_hosts)
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
