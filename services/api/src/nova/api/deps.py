"""FastAPI dependency wiring.

Long-lived objects (engine, session factory, Redis client, token service,
hasher) are built once during startup and parked on ``app.state``. Per-request
objects (session, repositories, services) are constructed here. Handlers only
ever ask for the service they need, so swapping an implementation is a change
in this module rather than across every route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from nova.ai.base import ChatProvider, EmbeddingProvider
from nova.core.config import Settings
from nova.core.errors import AuthenticationError
from nova.core.security import PasswordHasher, TokenService
from nova.db.session import session_scope
from nova.models.user import User
from nova.repositories.conversation import ConversationRepository, MessageRepository
from nova.repositories.memory import MemoryRepository
from nova.repositories.refresh_token import RefreshTokenRepository
from nova.repositories.tool_invocation import ToolInvocationRepository
from nova.repositories.user import UserRepository
from nova.services.auth import AuthService
from nova.services.conversation import ChatStreamer, ConversationService
from nova.services.health import HealthService
from nova.services.memory import MemoryExtractor, MemoryRecorder, MemoryService
from nova.services.rate_limit import RateLimiter
from nova.services.system import SystemStatusService
from nova.services.tools import ToolService
from nova.tools.registry import ToolRegistry

# auto_error=False so a missing header raises NOVA's AuthenticationError and
# takes the standard error envelope, rather than Starlette's bare 403.
_bearer_scheme = HTTPBearer(auto_error=False)


# -- application-scoped -------------------------------------------------------


def get_app_settings(request: Request) -> Settings:
    """Return the settings this app was built with.

    Read from ``app.state`` rather than calling ``get_settings()`` again, so
    an app constructed with explicit settings (tests, embedding) actually
    uses them instead of silently falling back to the environment.
    """
    return request.app.state.settings  # type: ignore[no-any-return]


def get_engine(request: Request) -> AsyncEngine:
    return request.app.state.engine  # type: ignore[no-any-return]


def get_redis(request: Request) -> Redis:
    return request.app.state.redis  # type: ignore[no-any-return]


def get_token_service(request: Request) -> TokenService:
    return request.app.state.token_service  # type: ignore[no-any-return]


def get_password_hasher(request: Request) -> PasswordHasher:
    return request.app.state.password_hasher  # type: ignore[no-any-return]


# -- request-scoped -----------------------------------------------------------


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield the request's unit of work.

    ``async with``, never ``async for``: when a handler raises, FastAPI
    throws that exception in here, and delegating with ``async for`` would
    abandon the scope mid-flight instead of unwinding it.
    """
    async with session_scope(request.app.state.session_factory) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_user_repository(session: SessionDep) -> UserRepository:
    return UserRepository(session)


def get_refresh_token_repository(session: SessionDep) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_tokens: Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)],
    hasher: Annotated[PasswordHasher, Depends(get_password_hasher)],
    tokens: Annotated[TokenService, Depends(get_token_service)],
) -> AuthService:
    return AuthService(users=users, refresh_tokens=refresh_tokens, hasher=hasher, tokens=tokens)


def get_health_service(
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> HealthService:
    return HealthService(engine=engine, redis=redis)


# -- tools --------------------------------------------------------------------


def get_tool_registry(request: Request) -> ToolRegistry:
    """The tools this process was started with.

    Built once during startup from configuration, never per request: which
    tools exist is a decision the machine's owner makes before any request
    arrives.
    """
    return request.app.state.tool_registry  # type: ignore[no-any-return]


def get_tool_invocation_repository(session: SessionDep) -> ToolInvocationRepository:
    return ToolInvocationRepository(session)


def get_tool_service(
    request: Request,
    registry: Annotated[ToolRegistry, Depends(get_tool_registry)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ToolService:
    """Build the tool service with the session *factory*, not a session.

    A tool called from the middle of a streamed reply runs after the
    request's unit of work has closed, and its audit row still has to be
    written. Same reason :func:`get_chat_streamer` takes the factory.
    """
    settings: Settings = request.app.state.settings
    return ToolService(
        registry=registry,
        settings=settings.tools,
        redis=redis,
        session_factory=request.app.state.session_factory,
    )


# -- AI providers -------------------------------------------------------------


def get_chat_provider(request: Request) -> ChatProvider:
    return request.app.state.chat_provider  # type: ignore[no-any-return]


def get_embedding_provider(request: Request) -> EmbeddingProvider:
    return request.app.state.embedding_provider  # type: ignore[no-any-return]


# -- memory -------------------------------------------------------------------


def get_memory_repository(session: SessionDep) -> MemoryRepository:
    return MemoryRepository(session)


def get_memory_service(
    request: Request,
    memories: Annotated[MemoryRepository, Depends(get_memory_repository)],
    embeddings: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
) -> MemoryService:
    settings: Settings = request.app.state.settings
    return MemoryService(memories=memories, embeddings=embeddings, settings=settings.ai)


def get_memory_recorder(
    request: Request,
    provider: Annotated[ChatProvider, Depends(get_chat_provider)],
    embeddings: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
) -> MemoryRecorder:
    """Build the recorder with the session factory, not a session.

    Same reason as :func:`get_chat_streamer`: it runs as a background task
    after the response has been sent, when the request session is closed.
    """
    settings: Settings = request.app.state.settings
    return MemoryRecorder(
        session_factory=request.app.state.session_factory,
        extractor=MemoryExtractor(provider=provider, settings=settings.ai),
        embeddings=embeddings,
        settings=settings.ai,
    )


# -- system status ------------------------------------------------------------


def get_system_status_service(
    request: Request,
    provider: Annotated[ChatProvider, Depends(get_chat_provider)],
    health: Annotated[HealthService, Depends(get_health_service)],
    registry: Annotated[ToolRegistry, Depends(get_tool_registry)],
    memories: Annotated[MemoryRepository, Depends(get_memory_repository)],
    invocations: Annotated[ToolInvocationRepository, Depends(get_tool_invocation_repository)],
) -> SystemStatusService:
    settings: Settings = request.app.state.settings
    return SystemStatusService(
        settings=settings,
        provider=provider,
        health=health,
        registry=registry,
        memories=memories,
        invocations=invocations,
    )


# -- conversations ------------------------------------------------------------


def get_conversation_repository(session: SessionDep) -> ConversationRepository:
    return ConversationRepository(session)


def get_message_repository(session: SessionDep) -> MessageRepository:
    return MessageRepository(session)


def get_conversation_service(
    request: Request,
    conversations: Annotated[ConversationRepository, Depends(get_conversation_repository)],
    messages: Annotated[MessageRepository, Depends(get_message_repository)],
    provider: Annotated[ChatProvider, Depends(get_chat_provider)],
    memories: Annotated[MemoryService, Depends(get_memory_service)],
) -> ConversationService:
    settings: Settings = request.app.state.settings
    return ConversationService(
        conversations=conversations,
        messages=messages,
        provider=provider,
        settings=settings.ai,
        memories=memories,
    )


def get_chat_streamer(
    request: Request,
    provider: Annotated[ChatProvider, Depends(get_chat_provider)],
    embeddings: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
    tools: Annotated[ToolService, Depends(get_tool_service)],
) -> ChatStreamer:
    """Build the streamer with the session *factory*, not a session.

    A streaming response body runs after the endpoint returns, by which point
    the request-scoped session is closed. The streamer opens its own -- and
    so does every tool it runs.
    """
    settings: Settings = request.app.state.settings
    return ChatStreamer(
        session_factory=request.app.state.session_factory,
        provider=provider,
        settings=settings.ai,
        embeddings=embeddings,
        tools=tools,
        tool_settings=settings.tools,
    )


def get_rate_limiter(redis: Annotated[Redis, Depends(get_redis)]) -> RateLimiter:
    return RateLimiter(redis)


# -- authentication -----------------------------------------------------------


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    tokens: Annotated[TokenService, Depends(get_token_service)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> User:
    """Resolve the bearer token to a live user.

    Raises:
        AuthenticationError: if the header is absent, the token does not
            verify, or the account no longer exists or is deactivated.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authorization header is missing.", code="missing_credentials")

    claims = tokens.decode_access_token(credentials.credentials)

    user = await users.get_by_id(claims.subject)
    # The database check is what makes deactivation take effect before the
    # access token would have expired on its own.
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.", code="account_unavailable")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def client_ip(request: Request) -> str | None:
    """Best-effort client address for audit rows and rate-limit keys."""
    return request.client.host if request.client else None


def request_id(request: Request) -> str | None:
    """The id :class:`RequestContextMiddleware` bound to this request.

    Carried into audit rows so a tool invocation can be traced back to the
    log lines for the request that caused it.
    """
    value = getattr(request.state, "request_id", None)
    return str(value) if value else None


def rate_limit_key(request: Request, *, scope: str, identifier: str | None = None) -> str:
    """Build a rate-limit key from a scope and the caller's address."""
    return f"{scope}:{identifier or client_ip(request) or 'unknown'}"


__all__ = [
    "CurrentUser",
    "SessionDep",
    "client_ip",
    "get_app_settings",
    "get_auth_service",
    "get_current_user",
    "get_engine",
    "get_health_service",
    "get_rate_limiter",
    "get_redis",
    "get_session",
    "get_system_status_service",
    "get_tool_invocation_repository",
    "get_tool_registry",
    "get_tool_service",
    "rate_limit_key",
    "request_id",
]
