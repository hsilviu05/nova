"""Conversation and message persistence."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, select

from nova.models.conversation import Conversation, Message
from nova.repositories.base import BaseRepository


class ConversationRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.conversation.Conversation` rows."""

    async def get_for_owner(
        self, conversation_id: uuid.UUID, owner_id: uuid.UUID
    ) -> Conversation | None:
        """Fetch a conversation only if ``owner_id`` owns it.

        Ownership is part of the query rather than a check afterwards, so a
        handler cannot forget it and leak another user's thread.
        """
        stmt = select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == owner_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_owner(self, owner_id: uuid.UUID, *, limit: int = 50) -> list[Conversation]:
        """Most recently active first."""
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == owner_id)
            .order_by(desc(Conversation.last_message_at), desc(Conversation.created_at))
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    def add(self, conversation: Conversation) -> Conversation:
        self._session.add(conversation)
        return conversation

    async def delete(self, conversation: Conversation) -> None:
        await self._session.delete(conversation)


class MessageRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.conversation.Message` rows."""

    async def list_for_conversation(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Message]:
        """Oldest first, which is replay order."""
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_recent(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        """The newest ``limit`` messages, returned oldest first.

        The model needs recent context in reading order, but "recent" has to
        be selected from the newest end. Fetching descending and reversing is
        one indexed query; ordering ascending with an offset would need a
        count first and would drift as the conversation grows.
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
        newest_first = list((await self._session.execute(stmt)).scalars().all())
        return list(reversed(newest_first))

    def add(self, message: Message) -> Message:
        self._session.add(message)
        return message
