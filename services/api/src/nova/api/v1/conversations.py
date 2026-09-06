"""Conversation routes.

The send endpoint streams Server-Sent Events. SSE rather than a WebSocket:
a reply is one-directional and short-lived, it survives ordinary HTTP
infrastructure, and ``URLSession`` handles it without a socket library. The
device WebSocket exists because that traffic is genuinely bidirectional and
long-lived; this is not.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse

from nova.ai.errors import AIProviderError
from nova.api.deps import (
    CurrentUser,
    get_app_settings,
    get_chat_streamer,
    get_conversation_service,
    get_rate_limiter,
    rate_limit_key,
)
from nova.core.config import Settings
from nova.core.logging import get_logger
from nova.schemas.conversation import (
    ConversationDetail,
    ConversationRead,
    CreateConversationRequest,
    MessageExchange,
    SendMessageRequest,
)
from nova.services.conversation import ChatStreamer, ConversationService
from nova.services.rate_limit import RateLimiter

logger = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

ServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
StreamerDep = Annotated[ChatStreamer, Depends(get_chat_streamer)]
RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def _event(name: str, payload: dict[str, object]) -> str:
    """Format one Server-Sent Event.

    Both a blank line terminator and compact JSON: a newline inside the data
    field would split it into two events.
    """
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.get("", response_model=list[ConversationRead], summary="List conversations")
async def list_conversations(
    current_user: CurrentUser, service: ServiceDep
) -> list[ConversationRead]:
    return await service.list_conversations(current_user.id)


@router.post(
    "",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
)
async def create_conversation(
    payload: CreateConversationRequest,
    current_user: CurrentUser,
    service: ServiceDep,
) -> ConversationRead:
    """Open an empty thread.

    The title is left unset: it is derived from the first message rather than
    asked for.
    """
    return await service.create_conversation(current_user.id, title=payload.title)


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    summary="A conversation and its messages",
)
async def read_conversation(
    conversation_id: uuid.UUID, current_user: CurrentUser, service: ServiceDep
) -> ConversationDetail:
    """Someone else's conversation reports as not found, not forbidden."""
    return await service.get_conversation(conversation_id, current_user.id)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a conversation",
)
async def delete_conversation(
    conversation_id: uuid.UUID, current_user: CurrentUser, service: ServiceDep
) -> None:
    await service.delete_conversation(conversation_id, current_user.id)


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageExchange,
    status_code=status.HTTP_201_CREATED,
    summary="Send a message and wait for the reply",
    responses={
        404: {"description": "Conversation not found."},
        503: {"description": "The AI provider is unavailable."},
    },
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    request: Request,
    current_user: CurrentUser,
    service: ServiceDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> MessageExchange:
    """Send a message and return both turns once the reply is complete.

    The non-streaming path, for clients that would rather have one JSON
    response than parse an event stream.
    """
    await _enforce_limit(limiter, settings, request, current_user.id)
    return await service.send_message(conversation_id, current_user.id, payload.content)


@router.post(
    "/{conversation_id}/stream",
    summary="Send a message and stream the reply",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Server-Sent Events: message, delta, done, error.",
        },
        404: {"description": "Conversation not found."},
    },
)
async def stream_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    request: Request,
    current_user: CurrentUser,
    streamer: StreamerDep,
    limiter: RateLimiterDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """Stream NOVA's reply as it is produced.

    Ownership is checked and the user's turn is stored *before* the response
    begins, so an unknown conversation is a normal 404 rather than an error
    event inside a stream the client has already started rendering.
    """
    await _enforce_limit(limiter, settings, request, current_user.id)

    user_message, history = await streamer.prepare(
        conversation_id, current_user.id, payload.content
    )

    async def events() -> AsyncIterator[str]:
        # Echo the stored user turn first so the client can replace its
        # optimistic copy with the real id.
        yield _event("message", user_message.model_dump(mode="json"))

        try:
            async for chunk in streamer.stream(conversation_id, history):
                yield _event("delta", {"text": chunk})
        except AIProviderError as exc:
            # Only reachable before any text was produced; the streamer
            # swallows a mid-stream failure and keeps the partial reply.
            logger.info("stream_failed", code=exc.code)
            yield _event("error", {"code": exc.code, "message": exc.message})
            return

        yield _event("done", {"conversation_id": str(conversation_id)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Stops nginx buffering the stream into one lump on delivery.
            "X-Accel-Buffering": "no",
        },
    )


async def _enforce_limit(
    limiter: RateLimiter, settings: Settings, request: Request, user_id: uuid.UUID
) -> None:
    """Limit per user, not per address.

    Every message costs real money at the provider, and the caller is
    authenticated here, so the account is the right subject rather than
    whichever network they happen to be on.
    """
    await limiter.check(
        rate_limit_key(request, scope="chat", identifier=str(user_id)),
        limit=settings.ai.message_rate_limit,
        window_seconds=settings.ai.message_rate_limit_window_seconds,
    )
