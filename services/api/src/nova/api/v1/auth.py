"""Authentication routes.

Handlers stay thin: read the request, call :class:`~nova.services.auth.AuthService`,
return the result. No business rules, no queries.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from nova.api.deps import (
    CurrentUser,
    client_ip,
    get_app_settings,
    get_auth_service,
    get_rate_limiter,
    rate_limit_key,
)
from nova.core.config import Settings
from nova.schemas.auth import (
    AuthenticatedUser,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from nova.services.auth import AuthService
from nova.services.rate_limit import RateLimiter

router = APIRouter(prefix="/auth", tags=["auth"])

AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


async def _enforce_limit(
    limiter: RateLimiter, settings: Settings, request: Request, scope: str
) -> None:
    """Apply the shared auth rate limit for ``scope``."""
    await limiter.check(
        rate_limit_key(request, scope=scope),
        limit=settings.security.auth_rate_limit_attempts,
        window_seconds=settings.security.auth_rate_limit_window_seconds,
    )


@router.post(
    "/register",
    response_model=AuthenticatedUser,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"description": "Email already registered."}},
)
async def register(
    payload: RegisterRequest,
    request: Request,
    auth: AuthServiceDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> AuthenticatedUser:
    """Register a new user and return a signed-in token pair."""
    await _enforce_limit(limiter, settings, request, "register")
    return await auth.register(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        user_agent=_user_agent(request),
        ip_address=client_ip(request),
    )


@router.post(
    "/login",
    response_model=AuthenticatedUser,
    summary="Exchange credentials for tokens",
    responses={401: {"description": "Incorrect email or password."}},
)
async def login(
    payload: LoginRequest,
    request: Request,
    auth: AuthServiceDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> AuthenticatedUser:
    """Authenticate and open a new session."""
    await _enforce_limit(limiter, settings, request, "login")
    return await auth.login(
        email=payload.email,
        password=payload.password,
        user_agent=_user_agent(request),
        ip_address=client_ip(request),
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate a refresh token",
    responses={401: {"description": "Refresh token is invalid or has expired."}},
)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    auth: AuthServiceDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> TokenPair:
    """Exchange a refresh token for a fresh pair.

    The presented token is retired. Replaying it afterwards revokes every
    session descended from the same login.
    """
    await _enforce_limit(limiter, settings, request, "refresh")
    return await auth.refresh(
        refresh_token=payload.refresh_token,
        user_agent=_user_agent(request),
        ip_address=client_ip(request),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Revoke a session",
)
async def logout(payload: LogoutRequest, auth: AuthServiceDep) -> None:
    """Revoke the token family behind the supplied refresh token.

    Idempotent, and returns 204 even for an unknown token so the response
    cannot be used to probe which tokens exist.
    """
    await auth.logout(refresh_token=payload.refresh_token)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Revoke every session for the current user",
)
async def logout_all(current_user: CurrentUser, auth: AuthServiceDep) -> None:
    """Sign the authenticated user out on every device."""
    await auth.revoke_all_sessions(current_user.id)
