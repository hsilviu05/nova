"""System status routes.

What the device endpoints used to be. ``/system/status`` is the dashboard's
single read, and ``/system/activity`` is the audit log as the app shows it.

Both are scoped to the signed-in person: the host figures are about one
machine, but the memory counts, the activity and the project checks are
theirs, and an account on a shared NOVA should not see another's history.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from nova.api.deps import CurrentUser, get_system_status_service, get_tool_invocation_repository
from nova.repositories.tool_invocation import ToolInvocationRepository
from nova.schemas.system import ActivityEntry, ActivityPage, SystemStatus
from nova.services.system import SystemStatusService

router = APIRouter(prefix="/system", tags=["system"])

StatusServiceDep = Annotated[SystemStatusService, Depends(get_system_status_service)]
InvocationsDep = Annotated[ToolInvocationRepository, Depends(get_tool_invocation_repository)]


@router.get(
    "/status",
    response_model=SystemStatus,
    summary="Everything the dashboard shows",
)
async def read_status(current_user: CurrentUser, service: StatusServiceDep) -> SystemStatus:
    """The AI provider, this machine, dependencies, projects, memory, tools.

    One response rather than six endpoints: the home screen needs all of it
    before it can render, and separate requests would be separate round trips
    over a phone's Wi-Fi and separate chances for the cards to disagree.

    Always 200. A project that is down, a model server that is not running,
    and a missing GitHub token are findings on this screen rather than
    failures of it.
    """
    return await service.overview(current_user.id)


@router.get(
    "/activity",
    response_model=ActivityPage,
    summary="What NOVA has done",
)
async def read_activity(
    current_user: CurrentUser,
    invocations: InvocationsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    tool_name: Annotated[str | None, Query(max_length=64)] = None,
) -> ActivityPage:
    """The audit log, newest first.

    Includes refusals and failures, not only successes. "What did NOVA try to
    do" is the question worth being able to answer.
    """
    rows = await invocations.list_for_owner(
        current_user.id, limit=limit, offset=offset, tool_name=tool_name
    )
    return ActivityPage(items=[ActivityEntry.model_validate(row) for row in rows])
