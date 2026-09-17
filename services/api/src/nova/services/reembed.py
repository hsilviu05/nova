"""Re-embedding every memory after the embedder changes.

Vectors from two embedders are not comparable, so a switch leaves the old
rows invisible to retrieval until they are rewritten in the new space. This
is the pass that rewrites them. It is all-or-nothing: every row is
re-embedded inside one transaction, so a model server that dies halfway
leaves the store exactly as it was, never half in each space.

Deliberately a service function rather than a route. Re-embedding a whole
store calls the model once per memory, which on a laptop can be minutes;
it belongs in a terminal that shows progress, not behind an HTTP timeout.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.ai.base import EmbeddingProvider
from nova.core.logging import get_logger
from nova.repositories.memory import MemoryRepository

logger = get_logger(__name__)

DEFAULT_BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class ReembedReport:
    """What the pass found and what it changed."""

    provider: str
    # Rows per provider name before the pass, so the operator can see what
    # they are migrating from.
    before: dict[str, int] = field(default_factory=dict)
    reembedded: int = 0
    dry_run: bool = False

    @property
    def stale_before(self) -> int:
        return sum(count for name, count in self.before.items() if name != self.provider)


async def reembed_all(
    session_factory: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingProvider,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> ReembedReport:
    """Rewrite every memory not embedded by ``embeddings`` in its space.

    The embedder is probed once before any row is touched, so an unreachable
    model server fails here with its own error rather than after an hour.
    ``progress`` is called with (done, total) after each batch.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    await embeddings.embed(["probe"])
    provider = embeddings.name

    async with session_factory() as session:
        repository = MemoryRepository(session)
        before = await repository.count_by_provider()
        total = sum(count for name, count in before.items() if name != provider)

        done = 0
        after: uuid.UUID | None = None
        while True:
            batch = await repository.list_stale(provider, after=after, limit=batch_size)
            if not batch:
                break
            vectors = await embeddings.embed([memory.content for memory in batch])
            for memory, vector in zip(batch, vectors, strict=True):
                memory.embedding = vector
                memory.embedding_provider = provider
            after = batch[-1].id
            done += len(batch)
            if progress is not None:
                progress(done, total)

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    # Debug rather than info: the only caller is the script, which reports
    # to the terminal itself, and an info line would land mid-progress-bar.
    logger.debug(
        "memories_reembedded", provider=provider, reembedded=done, dry_run=dry_run, before=before
    )
    return ReembedReport(provider=provider, before=before, reembedded=done, dry_run=dry_run)
