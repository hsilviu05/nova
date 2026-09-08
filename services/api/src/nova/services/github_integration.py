"""GitHub dev mode: managing the integration and handling a delivery.

The webhook endpoint is the only unauthenticated public URL in the API, so
``handle_delivery`` is written as a sequence of refusals, cheapest first:

    1. the integration id is unknown              -> 404
    2. the body is over the cap                   -> 413, before reading it
    3. the signature does not match the raw body  -> 401
    4. the delivery id was seen already           -> 200, ignored
    5. the integration is disabled                -> 200, ignored
    6. it is for a repository we do not watch     -> 200, ignored
    7. the event is not one NOVA reacts to        -> 200, ignored
    8. otherwise: a face and a sentence, to every connected device

Everything after the signature is a 200. GitHub retries non-2xx responses
with backoff for days, and "not interested" is not a reason to be retried.

What a delivery does *not* do is write telemetry. The dataset contract
(ADR 013) has two sources, the device and the phone, and a build result is
neither: it is something that happened to the owner, not something NOVA
observed. Folding it in would change what the ML label means. If GitHub
events ever become a feature, that is a third source and its own decision.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from nova.core.clock import utc_now
from nova.core.config import Settings
from nova.core.errors import AuthenticationError, NotFoundError, PayloadTooLargeError
from nova.models.github_integration import GitHubIntegration
from nova.repositories.device import DeviceRepository
from nova.repositories.github_integration import GitHubIntegrationRepository
from nova.schemas.github import (
    GitHubIntegrationCreated,
    GitHubIntegrationRead,
    GitHubIntegrationUpdate,
    WebhookAck,
)
from nova.schemas.protocol import (
    ExpressionCommand,
    ExpressionPayload,
    SpeakCommand,
    SpeakPayload,
)
from nova.services.connections import ConnectionRegistry
from nova.services.github_webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    Ignored,
    Reaction,
    react,
    repository_full_name,
    verify_signature,
)

logger = structlog.get_logger(__name__)

WEBHOOK_PATH = "/api/v1/integrations/github/webhook/{integration_id}"

# A delivery id is remembered this long. GitHub's own redelivery window is
# measured in days, but a replay that arrives a day later is a replay that
# only makes a face; the cost of forgetting is one spurious smile.
DELIVERY_TTL_SECONDS = 24 * 60 * 60


class GitHubIntegrationService:
    def __init__(
        self,
        *,
        integrations: GitHubIntegrationRepository,
        devices: DeviceRepository,
        connections: ConnectionRegistry,
        redis: Redis,
        settings: Settings,
    ) -> None:
        self._integrations = integrations
        self._devices = devices
        self._connections = connections
        self._redis = redis
        self._settings = settings

    # -- the user's side ------------------------------------------------------

    async def create_or_rotate(
        self, user_id: uuid.UUID, *, repository: str | None
    ) -> GitHubIntegrationCreated:
        """Create the integration, or replace its secret if one exists.

        Rotation is the only way to see a secret again, and it invalidates
        the old one immediately -- there is no grace period during which
        both verify, because that period would be indistinguishable from a
        leak that has not been noticed yet.
        """
        secret = secrets.token_urlsafe(32)
        existing = await self._integrations.get_for_user(user_id)
        if existing is None:
            existing = await self._integrations.add(
                GitHubIntegration(
                    user_id=user_id,
                    webhook_secret=secret,
                    repository=repository,
                    enabled=True,
                )
            )
            logger.info("github_integration_created", integration_id=str(existing.id))
        else:
            existing.webhook_secret = secret
            if repository is not None:
                existing.repository = repository
            existing.enabled = True
            await self._integrations.session.flush()
            logger.info("github_integration_rotated", integration_id=str(existing.id))

        read = self._read(existing)
        return GitHubIntegrationCreated(**read.model_dump(), secret=secret)

    async def get_for_user(self, user_id: uuid.UUID) -> GitHubIntegrationRead:
        integration = await self._integrations.get_for_user(user_id)
        if integration is None:
            raise NotFoundError("No GitHub integration.", code="github_integration_not_found")
        return self._read(integration)

    async def update_for_user(
        self, user_id: uuid.UUID, changes: GitHubIntegrationUpdate
    ) -> GitHubIntegrationRead:
        integration = await self._integrations.get_for_user(user_id)
        if integration is None:
            raise NotFoundError("No GitHub integration.", code="github_integration_not_found")
        fields = changes.model_dump(exclude_unset=True)
        if "repository" in fields:
            integration.repository = fields["repository"]
        if "enabled" in fields and fields["enabled"] is not None:
            integration.enabled = fields["enabled"]
        await self._integrations.session.flush()
        return self._read(integration)

    async def delete_for_user(self, user_id: uuid.UUID) -> None:
        if not await self._integrations.delete_for_user(user_id):
            raise NotFoundError("No GitHub integration.", code="github_integration_not_found")
        logger.info("github_integration_deleted", user_id=str(user_id))

    # -- GitHub's side ----------------------------------------------------------

    async def handle_delivery(
        self,
        integration_id: uuid.UUID,
        *,
        headers: Mapping[str, str],
        declared_length: int | None,
        read_body: Any,
    ) -> WebhookAck:
        """Verify and act on one delivery.

        ``read_body`` is an awaitable that produces the raw bytes. It is
        called only after the cheap refusals, so an unknown integration id
        or an oversized Content-Length never costs a body read.
        """
        integration = await self._integrations.get(integration_id)
        if integration is None:
            raise NotFoundError("Unknown integration.", code="github_integration_not_found")

        if declared_length is not None and declared_length > MAX_BODY_BYTES:
            raise PayloadTooLargeError()

        body: bytes = await read_body()
        if len(body) > MAX_BODY_BYTES:
            raise PayloadTooLargeError()

        if not verify_signature(integration.webhook_secret, body, headers.get(SIGNATURE_HEADER)):
            # No detail. The header was either absent or wrong, and telling
            # a caller which would tell them something.
            logger.warning("github_delivery_rejected", integration_id=str(integration_id))
            raise AuthenticationError("Invalid signature.", code="github_bad_signature")

        event = headers.get(EVENT_HEADER)
        delivery_id = headers.get(DELIVERY_HEADER)
        now = utc_now()

        if delivery_id and not await self._first_sight(integration_id, delivery_id):
            return WebhookAck(status="ignored", reason="duplicate delivery")

        if not integration.enabled:
            return WebhookAck(status="ignored", reason="integration disabled")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            # Signed by GitHub and still not JSON is not a thing that
            # happens; if it does, it is not worth a retry storm.
            return WebhookAck(status="ignored", reason="body is not JSON")
        if not isinstance(payload, dict):
            return WebhookAck(status="ignored", reason="body is not an object")

        watched = integration.repository
        actual = repository_full_name(payload)
        if watched is not None and actual != watched:
            return WebhookAck(status="ignored", reason="repository not watched")

        outcome = react(event, payload)
        if isinstance(outcome, Ignored):
            await self._integrations.record_delivery(
                integration, at=now, event=f"{event or 'unknown'}:ignored"
            )
            return WebhookAck(status="ignored", reason=outcome.reason)

        reached = await self._dispatch(integration.user_id, outcome)
        await self._integrations.record_delivery(integration, at=now, event=outcome.summary)
        logger.info(
            "github_reaction",
            integration_id=str(integration_id),
            reaction=outcome.kind,
            devices_reached=reached,
        )
        return WebhookAck(status="reacted", reaction=outcome.kind, devices_reached=reached)

    # -- internals --------------------------------------------------------------

    async def _first_sight(self, integration_id: uuid.UUID, delivery_id: str) -> bool:
        """True the first time a delivery id is seen; False on a replay.

        Fails open on a Redis outage, the same way the rate limiter does:
        the worst a replay can do is repeat a facial expression, and that
        is not worth refusing every delivery until Redis is back.
        """
        key = f"github:delivery:{integration_id}:{delivery_id[:128]}"
        try:
            stored = await self._redis.set(key, "1", nx=True, ex=DELIVERY_TTL_SECONDS)
        except RedisError as exc:
            logger.warning("github_dedupe_unavailable", error=str(exc))
            return True
        return bool(stored)

    async def _dispatch(self, user_id: uuid.UUID, reaction: Reaction) -> int:
        """Send the face and the line to every connected device the user
        owns. Offline devices are skipped, not queued: a "build's green"
        that arrives when the device reconnects an hour later is noise."""
        reached = 0
        for device in await self._devices.list_for_owner(user_id):
            face = ExpressionCommand(
                payload=ExpressionPayload(emotion=reaction.emotion, intensity=1.0)
            )
            if not await self._connections.send(device.id, face.model_dump(mode="json")):
                continue
            line = SpeakCommand(payload=SpeakPayload(text=reaction.line, emotion=reaction.emotion))
            await self._connections.send(device.id, line.model_dump(mode="json"))
            reached += 1
        return reached

    def _read(self, integration: GitHubIntegration) -> GitHubIntegrationRead:
        path = WEBHOOK_PATH.format(integration_id=integration.id)
        base = (self._settings.public_base_url or "").rstrip("/")
        return GitHubIntegrationRead(
            id=integration.id,
            repository=integration.repository,
            enabled=integration.enabled,
            webhook_url=f"{base}{path}",
            last_delivery_at=integration.last_delivery_at,
            last_event=integration.last_event,
            created_at=integration.created_at,
        )


__all__ = ["DELIVERY_TTL_SECONDS", "WEBHOOK_PATH", "GitHubIntegrationService", "datetime"]
