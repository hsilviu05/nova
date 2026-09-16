"""GitHub dev mode routes.

Four authenticated routes for the owner, and one public route for GitHub.
The public one takes the raw body: the signature is over the bytes GitHub
sent, so nothing may parse or re-serialise them before verification.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from nova.api.deps import CurrentUser, get_github_integration_service
from nova.schemas.github import (
    GitHubIntegrationCreate,
    GitHubIntegrationCreated,
    GitHubIntegrationRead,
    GitHubIntegrationUpdate,
    WebhookAck,
)
from nova.services.github_integration import GitHubIntegrationService

router = APIRouter(prefix="/integrations/github", tags=["integrations"])

ServiceDep = Annotated[GitHubIntegrationService, Depends(get_github_integration_service)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=GitHubIntegrationCreated)
async def create_or_rotate(
    body: GitHubIntegrationCreate, user: CurrentUser, service: ServiceDep
) -> GitHubIntegrationCreated:
    """Create the integration, or rotate its secret. The secret is in this
    response and in no other."""
    return await service.create_or_rotate(user.id, repository=body.repository)


@router.get("", response_model=GitHubIntegrationRead)
async def read(user: CurrentUser, service: ServiceDep) -> GitHubIntegrationRead:
    return await service.get_for_user(user.id)


@router.patch("", response_model=GitHubIntegrationRead)
async def update(
    body: GitHubIntegrationUpdate, user: CurrentUser, service: ServiceDep
) -> GitHubIntegrationRead:
    return await service.update_for_user(user.id, body)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete(user: CurrentUser, service: ServiceDep) -> None:
    await service.delete_for_user(user.id)


@router.post(
    "/webhook/{integration_id}",
    response_model=WebhookAck,
    # Not in the user-facing docs' auth scheme: GitHub calls this, with a
    # signature rather than a bearer token.
    openapi_extra={"security": []},
)
async def webhook(integration_id: uuid.UUID, request: Request, service: ServiceDep) -> WebhookAck:
    length = request.headers.get("content-length")
    declared = int(length) if length is not None and length.isdigit() else None
    return await service.handle_delivery(
        integration_id,
        headers=request.headers,
        declared_length=declared,
        read_body=request.body,
    )
