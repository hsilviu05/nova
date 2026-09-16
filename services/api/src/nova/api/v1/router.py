"""Version 1 API router."""

from __future__ import annotations

from fastapi import APIRouter

from nova.api.v1 import (
    auth,
    conversations,
    integrations,
    memories,
    system,
    tools,
    users,
)

router = APIRouter()
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(conversations.router)
router.include_router(memories.router)
# Tools and system status are two halves of one idea -- what NOVA can do, and
# what it can see -- but they are separate prefixes because one is a catalogue
# of actions and the other is a view of state.
router.include_router(tools.router)
router.include_router(system.router)
# GitHub reaches inward rather than being polled: a signed webhook telling
# NOVA a build finished. The device socket that used to carry the reaction is
# gone, so the result lands in the activity feed instead.
router.include_router(integrations.router)
