"""User routes."""

from __future__ import annotations

from fastapi import APIRouter

from nova.api.deps import CurrentUser, SessionDep
from nova.schemas.user import UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead, summary="Current user")
async def read_me(current_user: CurrentUser) -> UserRead:
    """Return the authenticated user's profile."""
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead, summary="Update current user")
async def update_me(
    payload: UserUpdate, current_user: CurrentUser, session: SessionDep
) -> UserRead:
    """Update the authenticated user's mutable profile fields."""
    if payload.display_name is not None:
        current_user.display_name = payload.display_name.strip()
    if payload.timezone is not None:
        current_user.timezone = payload.timezone
    await session.flush()
    return UserRead.model_validate(current_user)
