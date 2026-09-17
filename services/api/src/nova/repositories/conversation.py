"""Conversation and message persistence."""

from __future__ import annotations

import uuid

from sqlalchemy import desc, or_, select

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

    async def search_for_owner(
        self, owner_id: uuid.UUID, query: str, *, limit: int = 50
    ) -> list[tuple[Conversation, str | None]]:
        """Conversations whose title or any message contains ``query``.

        Each result carries the newest matching message's content, or None
        when only the title matched, so the caller can show where the hit
        was. Case-insensitive substring match: the wildcard characters in
        the query are escaped, so searching for "100%" finds "100%" and not
        everything.
        """
        pattern = "%" + _escape_like(query) + "%"

        # The newest matching message per conversation, restricted to this
        # owner's threads so the trigram scan never touches anyone else's.
        matched = (
            select(Message.conversation_id, Message.content)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.user_id == owner_id,
                Message.content.ilike(pattern, escape="\\"),
            )
            .distinct(Message.conversation_id)
            .order_by(Message.conversation_id, desc(Message.created_at))
            .subquery()
        )
        stmt = (
            select(Conversation, matched.c.content)
            .outerjoin(matched, matched.c.conversation_id == Conversation.id)
            .where(
                Conversation.user_id == owner_id,
                or_(
                    Conversation.title.ilike(pattern, escape="\\"),
                    matched.c.content.is_not(None),
                ),
            )
            .order_by(desc(Conversation.last_message_at), desc(Conversation.created_at))
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    def add(self, conversation: Conversation) -> Conversation:
        self._session.add(conversation)
        return conversation

    async def delete(self, conversation: Conversation) -> None:
        await self._session.delete(conversation)


class MessageRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.conversation.Message` rows."""

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


def _escape_like(text: str) -> str:
    """Make ``text`` match itself literally inside a LIKE pattern."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
