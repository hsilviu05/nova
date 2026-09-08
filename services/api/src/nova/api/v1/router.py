"""Version 1 API router."""

from __future__ import annotations

from fastapi import APIRouter

from nova.api.v1 import (
    analytics,
    auth,
    conversations,
    device_ws,
    devices,
    integrations,
    memories,
    users,
)

router = APIRouter()
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(devices.router)
# Same prefix as devices: these are facts about a device, not a separate
# resource, and splitting them would make the URL lie about that.
router.include_router(analytics.router)
router.include_router(conversations.router)
router.include_router(memories.router)
router.include_router(integrations.router)
# The device socket lives under the same version prefix as the REST routes it
# shares a protocol version with.
router.include_router(device_ws.router)
