"""GitHub dev mode: the integration a user manages, and the webhook's reply."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# "owner/name". GitHub's own limits are looser; this is what a repository
# name looks like and nothing else needs to pass.
RepositoryName = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)


class GitHubIntegrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # When set, deliveries from any other repository are acknowledged and
    # ignored. Optional because a hook on a single repository already
    # limits itself.
    repository: str | None = RepositoryName


class GitHubIntegrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str | None = RepositoryName
    enabled: bool | None = None


class GitHubIntegrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository: str | None
    enabled: bool
    # Absolute when the server knows its public name, otherwise a path.
    webhook_url: str
    last_delivery_at: datetime | None
    last_event: str | None
    created_at: datetime


class GitHubIntegrationCreated(GitHubIntegrationRead):
    """The one response that carries the secret.

    It is shown here and never again. Paste it into the webhook's "Secret"
    field on GitHub; losing it means rotating it.
    """

    secret: str
    content_type: Literal["application/json"] = "application/json"


class WebhookAck(BaseModel):
    """What the webhook endpoint tells GitHub.

    Always a 200 once the signature has passed. GitHub retries anything
    else, and "we chose not to react" is not something to retry.
    """

    status: Literal["reacted", "ignored"]
    reason: str | None = None
    reaction: str | None = None
    devices_reached: int = 0
