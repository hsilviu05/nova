"""Memory routes.

These exist for a reason beyond completeness. NOVA forms and keeps beliefs
about the person it sits with, drawn from things they said in passing. Read,
correct, and delete are what make that acceptable rather than unnerving, so
they are part of the feature, not an admin surface bolted on afterwards.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from nova.api.deps import CurrentUser, get_memory_service
from nova.schemas.memory import (
    MemoryCategory,
    MemoryPage,
    MemoryRead,
    MemorySearchResult,
    MemoryUpdate,
)
from nova.services.memory import MemoryService

router = APIRouter(prefix="/memories", tags=["memories"])

ServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


@router.get("", response_model=MemoryPage, summary="What NOVA remembers about you")
async def list_memories(
    current_user: CurrentUser,
    service: ServiceDep,
    category: MemoryCategory | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryPage:
    """Most important first, then most recent."""
    return MemoryPage(
        items=await service.list_memories(
            current_user.id, category=category, limit=limit, offset=offset
        ),
        total=await service.count(current_user.id, category=category),
    )


@router.get(
    "/search",
    response_model=list[MemorySearchResult],
    summary="Search memories by meaning",
)
async def search_memories(
    current_user: CurrentUser,
    service: ServiceDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
) -> list[MemorySearchResult]:
    """The same retrieval a reply uses, exposed with its scores.

    Making it visible is what lets someone judge whether NOVA is recalling
    the right things, rather than inferring it from how replies feel.
    """
    matches = await service.retrieve(current_user.id, q)
    return [
        MemorySearchResult(
            memory=MemoryRead.model_validate(match.memory),
            similarity=match.similarity,
        )
        for match in matches
    ]


@router.patch(
    "/{memory_id}",
    response_model=MemoryRead,
    summary="Correct a memory",
    responses={404: {"description": "Memory not found."}},
)
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdate,
    current_user: CurrentUser,
    service: ServiceDep,
) -> MemoryRead:
    """Editing the text re-embeds it, so retrieval follows the correction."""
    return await service.update(memory_id, current_user.id, payload)


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a memory",
    responses={404: {"description": "Memory not found."}},
)
async def delete_memory(
    memory_id: uuid.UUID, current_user: CurrentUser, service: ServiceDep
) -> None:
    await service.delete(memory_id, current_user.id)


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Forget everything",
)
async def delete_all_memories(current_user: CurrentUser, service: ServiceDep) -> None:
    """Clear every memory without deleting the account.

    Someone may want NOVA to stop knowing things about them and still keep
    their device and their conversations.
    """
    await service.delete_all(current_user.id)
