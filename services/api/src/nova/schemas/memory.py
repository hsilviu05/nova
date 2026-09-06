"""Memory payloads.

The embedding is deliberately absent from every schema here. It is an
implementation detail of retrieval, it is 1536 floats, and a client that
received it could do nothing with it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.models.memory import CATEGORIES

# Mirrors the model's check constraint. Declared as a Literal so an unknown
# category is a 422 with a useful message rather than a database error.
MemoryCategory = Literal["preference", "fact", "routine", "relationship", "project", "event"]

MAX_MEMORY_CONTENT = 300


class MemoryRead(BaseModel):
    """One thing NOVA remembers, as the owner sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    category: str
    importance: float
    confidence: float
    # Where it came from, so "why does it think that?" has an answer.
    source_conversation_id: uuid.UUID | None
    recall_count: int
    last_recalled_at: datetime | None
    created_at: datetime


class MemoryPage(BaseModel):
    """A page of memories with the total, so the app can show a count."""

    items: list[MemoryRead]
    total: int


class MemoryUpdate(BaseModel):
    """A correction from the person the memory is about.

    Every field is optional and unset means unchanged, so the app can send
    just what was edited.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=MAX_MEMORY_CONTENT)
    category: MemoryCategory | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("content")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        if value is None:
            return None
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("Memory content must not be blank.")
        return collapsed


class MemorySearchResult(BaseModel):
    """A memory with how well it matched a query."""

    memory: MemoryRead
    # 1.0 is identical, 0.0 unrelated. Exposed because a retrieval screen
    # that shows scores is how someone judges whether NOVA is recalling the
    # right things.
    similarity: float


def _assert_categories_match() -> None:
    """Fail at import if the Literal has drifted from the model's tuple.

    The two have to be written out separately -- one is a type, the other a
    runtime tuple -- so this is what stops a category being added in one
    place and silently rejected by the other.
    """
    declared = set(MemoryCategory.__args__)  # type: ignore[attr-defined]
    if declared != set(CATEGORIES):  # pragma: no cover - unreachable while correct
        raise RuntimeError(
            f"MemoryCategory {sorted(declared)} does not match "
            f"nova.models.memory.CATEGORIES {sorted(CATEGORIES)}"
        )


_assert_categories_match()
