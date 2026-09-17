"""Zero-padding a narrower embedder: lossless for cosine, refused for wider."""

from __future__ import annotations

import math

import pytest

from nova.ai.errors import AIConfigurationError
from nova.ai.offline import OfflineEmbeddingProvider
from nova.ai.padded import PaddedEmbeddingProvider, fit, pad


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class TestPad:
    def test_appends_zeros_to_the_width(self) -> None:
        assert pad([1.0, 2.0], 5) == [1.0, 2.0, 0.0, 0.0, 0.0]

    def test_an_exact_fit_is_returned_as_is(self) -> None:
        vector = [1.0, 2.0]
        assert pad(vector, 2) is vector

    def test_a_wider_vector_is_refused_not_truncated(self) -> None:
        with pytest.raises(AIConfigurationError):
            pad([1.0, 2.0, 3.0], 2)

    def test_cosine_distance_survives_padding_exactly(self) -> None:
        """The arithmetic the whole design rests on: appending zeros adds
        zero to the dot product and zero to each norm."""
        a = [0.3, -1.2, 0.7, 2.0]
        b = [1.1, 0.4, -0.5, 0.9]

        assert _cosine(pad(a, 16), pad(b, 16)) == pytest.approx(_cosine(a, b), abs=1e-12)


class TestPaddedProvider:
    async def test_widens_every_vector_and_keeps_the_name(self) -> None:
        provider = PaddedEmbeddingProvider(OfflineEmbeddingProvider(dimensions=64), width=256)

        vectors = await provider.embed(["a", "b"])

        assert provider.name == OfflineEmbeddingProvider(dimensions=64).name
        assert provider.dimensions == 256
        assert provider.native_dimensions == 64
        assert [len(v) for v in vectors] == [256, 256]
        assert all(v[64:] == [0.0] * 192 for v in vectors)

    def test_refuses_an_inner_provider_wider_than_the_column(self) -> None:
        with pytest.raises(AIConfigurationError) as caught:
            PaddedEmbeddingProvider(OfflineEmbeddingProvider(dimensions=512), width=256)
        assert caught.value.code == "ai_embedding_dimension_mismatch"

    def test_fit_leaves_an_exact_match_unwrapped(self) -> None:
        inner = OfflineEmbeddingProvider(dimensions=128)
        assert fit(inner, width=128) is inner
        assert isinstance(fit(inner, width=256), PaddedEmbeddingProvider)
