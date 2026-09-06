"""Version 1 API router."""

from __future__ import annotations

from fastapi import APIRouter

from nova.api.v1 import auth, users

router = APIRouter()
router.include_router(auth.router)
router.include_router(users.router)
