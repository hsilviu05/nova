"""Conversation payloads."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Long enough for a paragraph dictated by voice, short enough that a single
# turn cannot blow out the context window or the bill.
MAX_MESSAGE_LENGTH = 4000


class MessageRead(BaseModel):
    """One stored turn."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    model: str | None = None
    latency_ms: int | None = None


class ConversationRead(BaseModel):
    """A thread, without its messages."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    message_count: int
    last_message_at: datetime | None
    created_at: datetime


class ConversationDetail(ConversationRead):
    """A thread with its newest messages, oldest first.

    ``messages`` is capped at the newest 200; ``message_count`` is the real
    total, so ``len(messages) < message_count`` means older ones were left out.
    """

    messages: list[MessageRead] = Field(default_factory=list)


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120)


class SendMessageRequest(BaseModel):
    """Something said to NOVA."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("content")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message must not be blank.")
        return stripped


class MessageExchange(BaseModel):
    """Both halves of a completed non-streaming exchange."""

    user_message: MessageRead
    assistant_message: MessageRead
