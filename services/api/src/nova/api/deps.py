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

from nova.core.config import Settings
from nova.core.errors import AuthenticationError
from nova.core.security import PasswordHasher, TokenService
from nova.db.session import session_scope
from nova.models.user import User
from nova.repositories.device import (
    DeviceClaimRepository,
    DeviceCredentialRepository,
    DeviceRepository,
    DeviceTelemetryRepository,
)
from nova.repositories.refresh_token import RefreshTokenRepository
from nova.repositories.user import UserRepository
from nova.services.auth import AuthService
from nova.services.connections import ConnectionRegistry
from nova.services.device import DeviceService
from nova.services.health import HealthService
from nova.services.provisioning import ProvisioningService
from nova.services.rate_limit import RateLimiter

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
    """Yield the request's unit of work."""
    async for session in session_scope(request.app.state.session_factory):
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


# -- devices ------------------------------------------------------------------


def get_connection_registry(request: Request) -> ConnectionRegistry:
    return request.app.state.connections  # type: ignore[no-any-return]


def get_device_repository(session: SessionDep) -> DeviceRepository:
    return DeviceRepository(session)


def get_device_credential_repository(
    session: SessionDep,
) -> DeviceCredentialRepository:
    return DeviceCredentialRepository(session)


def get_device_claim_repository(session: SessionDep) -> DeviceClaimRepository:
    return DeviceClaimRepository(session)


def get_device_telemetry_repository(session: SessionDep) -> DeviceTelemetryRepository:
    return DeviceTelemetryRepository(session)


def get_provisioning_service(
    request: Request,
    devices: Annotated[DeviceRepository, Depends(get_device_repository)],
    credentials: Annotated[DeviceCredentialRepository, Depends(get_device_credential_repository)],
    claims: Annotated[DeviceClaimRepository, Depends(get_device_claim_repository)],
) -> ProvisioningService:
    settings: Settings = request.app.state.settings
    return ProvisioningService(
        devices=devices,
        credentials=credentials,
        claims=claims,
        settings=settings.device,
    )


def get_device_service(
    devices: Annotated[DeviceRepository, Depends(get_device_repository)],
    credentials: Annotated[DeviceCredentialRepository, Depends(get_device_credential_repository)],
    telemetry: Annotated[DeviceTelemetryRepository, Depends(get_device_telemetry_repository)],
    connections: Annotated[ConnectionRegistry, Depends(get_connection_registry)],
) -> DeviceService:
    return DeviceService(
        devices=devices,
        credentials=credentials,
        telemetry=telemetry,
        connections=connections,
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
    "rate_limit_key",
]
