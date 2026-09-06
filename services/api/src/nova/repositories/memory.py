"""Memory persistence and similarity search."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, desc, func, select, update

from nova.models.memory import Memory
from nova.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """A memory with how far it sat from the query.

    Cosine *distance*, so smaller is closer: 0 is identical, 1 is unrelated,
    2 is opposite. Carried alongside the row rather than stored on it,
    because the score is a property of one search, not of the memory.
    """

    memory: Memory
    distance: float

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


class MemoryRepository(BaseRepository):
    """Reads and writes :class:`~nova.models.memory.Memory` rows."""

    async def get_for_owner(self, memory_id: uuid.UUID, owner_id: uuid.UUID) -> Memory | None:
        """Ownership is part of the query, not a check afterwards."""
        stmt = select(Memory).where(Memory.id == memory_id, Memory.user_id == owner_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_owner(
        self,
        owner_id: uuid.UUID,
        *,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        """Most important first, then most recent.

        Importance leads because the memory screen is something a person
        skims to check what NOVA believes, and what it weights most is what
        they most need to see -- and correct.
        """
        stmt = select(Memory).where(Memory.user_id == owner_id)
        if category is not None:
            stmt = stmt.where(Memory.category == category)

        stmt = (
            stmt.order_by(desc(Memory.importance), desc(Memory.created_at))
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_for_owner(self, owner_id: uuid.UUID, *, category: str | None = None) -> int:
        stmt = select(func.count()).select_from(Memory).where(Memory.user_id == owner_id)
        if category is not None:
            stmt = stmt.where(Memory.category == category)
        return int((await self._session.execute(stmt)).scalar_one())

    async def search(
        self,
        owner_id: uuid.UUID,
        embedding: list[float],
        *,
        limit: int,
        max_distance: float,
    ) -> list[ScoredMemory]:
        """Nearest memories to ``embedding``, owned by ``owner_id``.

        The owner filter is in the WHERE clause, so one account's memories
        can never surface in another's context. That is a correctness
        boundary, not an optimisation: the retrieved text goes straight into
        a prompt.

        ``max_distance`` drops results that merely happen to be nearest.
        Without it a search always returns ``limit`` rows, however unrelated,
        and unrelated memories in the prompt make NOVA sound like it is
        confusing people.
        """
        if limit <= 0:
            return []

        distance = Memory.embedding.cosine_distance(embedding).label("distance")
        stmt = (
            select(Memory, distance)
            .where(Memory.user_id == owner_id, distance <= max_distance)
            .order_by(distance)
            .limit(limit)
        )

        rows = (await self._session.execute(stmt)).all()
        return [ScoredMemory(memory=row[0], distance=float(row[1])) for row in rows]

    async def find_similar(
        self, owner_id: uuid.UUID, embedding: list[float], *, threshold: float
    ) -> Memory | None:
        """The nearest memory, if it is near enough to be the same thing.

        Used to stop a fact being stored again in slightly different words
        every time it comes up -- without it, "drinks coffee" accumulates a
        dozen near-duplicates that crowd out everything else at retrieval.
        """
        matches = await self.search(owner_id, embedding, limit=1, max_distance=threshold)
        return matches[0].memory if matches else None

    def add(self, memory: Memory) -> Memory:
        self._session.add(memory)
        return memory

    async def delete(self, memory: Memory) -> None:
        await self._session.delete(memory)

    async def delete_for_owner(self, owner_id: uuid.UUID) -> int:
        """Forget everything about one person. Returns how many rows went.

        A bulk DELETE rather than loading every row and deleting it through
        the session: this is a privacy operation, and it should not get
        slower -- or start failing on memory -- the more NOVA knows.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(Memory).where(Memory.user_id == owner_id)),
        )
        return result.rowcount or 0

    async def record_recall(self, memory_ids: list[uuid.UUID], *, at: datetime) -> None:
        """Note that these memories were used.

        Feeds the analytics phase, and eventually a prune: a memory never
        recalled in months is a candidate for removal in a way a frequently
        used one is not.
        """
        if not memory_ids:
            return

        stmt = (
            update(Memory)
            .where(Memory.id.in_(memory_ids))
            .values(recall_count=Memory.recall_count + 1, last_recalled_at=at)
        )
        await self._session.execute(stmt)
