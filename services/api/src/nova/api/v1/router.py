"""Version 1 API router."""

from __future__ import annotations

from fastapi import APIRouter

from nova.api.v1 import auth, conversations, device_ws, devices, users

router = APIRouter()
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(devices.router)
router.include_router(conversations.router)
# The device socket lives under the same version prefix as the REST routes it
# shares a protocol version with.
router.include_router(device_ws.router)
