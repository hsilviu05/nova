"""Zero-padding a narrower embedder up to the memories column.

The column's width is a ceiling rather than an exact requirement, and the
reason it can be is arithmetic: cosine distance between two vectors is
unchanged by appending zeros to both. The dot product gains only zero terms,
and so do the squared norms, so every angle the index compares is exactly
the angle the model produced. Padding is lossless. Truncation is not, and
is refused.

What padding does *not* do is make vectors from different models
comparable. That is what the ``embedding_provider`` column and the
re-embedding pass are for.
"""

from __future__ import annotations

from nova.ai.base import EmbeddingProvider
from nova.ai.errors import AIConfigurationError


class PaddedEmbeddingProvider:
    """An embedder whose vectors are widened with zeros to ``width``."""

    def __init__(self, inner: EmbeddingProvider, *, width: int) -> None:
        if inner.dimensions > width:
            raise AIConfigurationError(
                f"Embedding model produces {inner.dimensions} dimensions, wider than the "
                f"memories column ({width}). Truncating would destroy the geometry retrieval "
                "depends on; widen the column with a migration instead.",
                code="ai_embedding_dimension_mismatch",
            )
        self._inner = inner
        self._width = width

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def dimensions(self) -> int:
        return self._width

    @property
    def native_dimensions(self) -> int:
        """What the model actually produces, before padding."""
        return self._inner.dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = await self._inner.embed(texts)
        return [pad(vector, self._width) for vector in vectors]

    async def aclose(self) -> None:
        await self._inner.aclose()


def pad(vector: list[float], width: int) -> list[float]:
    """``vector`` followed by enough zeros to reach ``width``."""
    if len(vector) > width:
        raise AIConfigurationError(
            f"Embedding of {len(vector)} dimensions does not fit a column of {width}.",
            code="ai_embedding_dimension_mismatch",
        )
    if len(vector) == width:
        return vector
    return vector + [0.0] * (width - len(vector))


def fit(inner: EmbeddingProvider, *, width: int) -> EmbeddingProvider:
    """``inner`` unchanged when it already fills ``width``, padded otherwise."""
    if inner.dimensions == width:
        return inner
    return PaddedEmbeddingProvider(inner, width=width)
