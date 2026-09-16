"""Tool routes.

Two endpoints: what NOVA can do, and doing one of them. The second is the
app's direct path -- the Tools screen, and the "Yes, go ahead" button on a
confirmation raised during a conversation.

A destructive tool invoked without a confirmation token answers 409 with the
token to present next. That is the whole handshake: no state machine, no
pending-action table, and a token that expires on its own if nobody answers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from nova.api.deps import (
    CurrentUser,
    get_app_settings,
    get_rate_limiter,
    get_tool_service,
    rate_limit_key,
    request_id,
)
from nova.core.config import Settings
from nova.schemas.tool import (
    InvokeToolRequest,
    ToolListResponse,
    ToolRead,
    ToolResultRead,
)
from nova.services.rate_limit import RateLimiter
from nova.services.tools import ToolService, tool_context

router = APIRouter(prefix="/tools", tags=["tools"])

ToolServiceDep = Annotated[ToolService, Depends(get_tool_service)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@router.get("", response_model=ToolListResponse, summary="What NOVA can do here")
async def list_tools(
    current_user: CurrentUser, tools: ToolServiceDep, settings: SettingsDep
) -> ToolListResponse:
    """Every tool registered on this machine, with its schema.

    The permission is included, unlike in what the model is shown: someone
    about to press a button is entitled to know whether it changes anything.
    """
    return ToolListResponse(
        items=[
            ToolRead(
                name=spec.name,
                description=spec.description,
                group=spec.group.value,
                permission=spec.permission.value,
                requires_confirmation=spec.permission.needs_confirmation,
                input_schema=spec.input_schema,
            )
            for spec in tools.specs()
        ],
        shell_enabled=settings.tools.shell_enabled,
    )


@router.post(
    "/invoke",
    response_model=ToolResultRead,
    status_code=status.HTTP_200_OK,
    summary="Run a tool",
    responses={
        403: {"description": "The tool is not permitted here."},
        404: {"description": "No such tool on this machine."},
        409: {
            "description": (
                "The tool changes something. The body carries a confirmation "
                "token and the prompt to show; send the same request again "
                "with the token to go ahead."
            )
        },
        422: {"description": "The arguments do not fit the tool's schema."},
        504: {"description": "The tool overran its time budget."},
    },
)
async def invoke_tool(
    payload: InvokeToolRequest,
    request: Request,
    current_user: CurrentUser,
    tools: ToolServiceDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> ToolResultRead:
    """Run one tool as this person.

    Rate limited per account rather than per address: tools spawn processes
    and reach other services, so the cost is the machine's, and the caller is
    authenticated here anyway.
    """
    await limiter.check(
        rate_limit_key(request, scope="tools", identifier=str(current_user.id)),
        limit=settings.tools.tool_rate_limit,
        window_seconds=settings.tools.tool_rate_limit_window_seconds,
    )

    invocation = await tools.invoke(
        payload.name,
        payload.arguments,
        tool_context(
            user_id=current_user.id,
            request_id=request_id(request),
            # A person pressed a button. The audit log distinguishes this
            # from the model asking, and the confirmation gate reads it.
            initiated_by_model=False,
        ),
        confirmation_token=payload.confirmation_token,
    )

    return ToolResultRead(
        tool=invocation.tool_name,
        content=invocation.result.content,
        data=invocation.result.data,
        is_error=invocation.result.is_error,
        truncated=invocation.result.truncated,
        duration_ms=invocation.duration_ms,
    )
