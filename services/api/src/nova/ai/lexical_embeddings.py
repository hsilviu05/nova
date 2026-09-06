"""A local embedding provider that actually retrieves.

Anthropic has no embeddings endpoint, which is precisely why ADR 002 gave
embeddings their own interface: a real embedder is a different vendor
(Voyage, a hosted alternative, or a local transformer) plugged in behind
:class:`~nova.ai.base.EmbeddingProvider`.

Until one is configured, this is what NOVA uses. It is the hashing trick --
a bag of word and character n-grams, signed-hashed into a fixed-width vector,
sublinearly weighted and L2-normalised -- so cosine similarity measures
**shared vocabulary**.

Be precise about what that buys and what it does not:

* it *does* rank "coffee every morning" above "the deployment failed" for the
  query "I drink coffee in the morning", and character n-grams make it
  tolerant of morphology (drink/drinking, run/running);
* it *does not* know that "espresso" relates to "coffee", or that "my partner"
  and "my wife" refer to the same person. Nothing here has read anything.

So retrieval works and is demonstrable, but its *quality* ceiling is lexical.
Judging semantic recall needs a real embedder, and swapping one in is a
configuration change plus a re-embedding pass -- not a rewrite.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

# Word-ish tokens. Deliberately permissive: apostrophes and digits are kept
# because "don't" and "8am" carry meaning in the things people tell NOVA.
_TOKEN = re.compile(r"[a-z0-9']+")

# Character n-grams bridge morphology that whole-word matching misses --
# "running" and "runs" share "run" but no whole token.
_NGRAM_SIZE = 4

# Words too common to carry signal. Short list on purpose: an aggressive stop
# list throws away "not", which inverts meaning.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "she",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
    ]
)


class LexicalEmbeddingProvider:
    """Hash-based lexical embeddings. Deterministic, offline, no model."""

    def __init__(self, *, dimensions: int = 1536, use_ngrams: bool = True) -> None:
        self._dimensions = dimensions
        self._use_ngrams = use_ngrams

    @property
    def name(self) -> str:
        return "lexical"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    # -- internals --------------------------------------------------------

    def _features(self, text: str) -> Counter[str]:
        """Bag of features for one piece of text."""
        lowered = text.lower()
        words = [token for token in _TOKEN.findall(lowered) if token not in _STOPWORDS]

        features: Counter[str] = Counter(words)

        if self._use_ngrams:
            # Padded so that word boundaries themselves are signal, which is
            # what makes short words match at all.
            for word in words:
                if len(word) < _NGRAM_SIZE:
                    features[f"#{word}#"] += 1
                    continue
                padded = f"#{word}#"
                for index in range(len(padded) - _NGRAM_SIZE + 1):
                    features[padded[index : index + _NGRAM_SIZE]] += 1

        return features

    def _vector(self, text: str) -> list[float]:
        features = self._features(text)
        vector = [0.0] * self._dimensions

        for feature, count in features.items():
            bucket, sign = self._bucket(feature)
            # Sublinear term frequency: the tenth mention of a word says much
            # less than the second, and raw counts let one repeated token
            # dominate the whole vector.
            vector[bucket] += sign * (1.0 + math.log(count))

        return self._normalise(vector)

    def _bucket(self, feature: str) -> tuple[int, int]:
        """Map a feature to a bucket and a sign.

        Signed hashing so that collisions tend to cancel rather than
        accumulate, which keeps an unrelated feature from inflating a
        similarity score.
        """
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % self._dimensions, 1 if (value >> 63) & 1 else -1

    @staticmethod
    def _normalise(vector: list[float]) -> list[float]:
        """Scale to unit length so cosine distance is comparable.

        An all-zero vector -- empty text, or nothing but stopwords -- is left
        as zeros rather than divided by zero. pgvector treats that as maximally
        distant from everything, which is the right answer for content that
        says nothing.
        """
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0.0:
            return vector
        return [value / magnitude for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Similarity between two unit-length vectors.

    Used in tests and diagnostics; retrieval itself happens in Postgres,
    where pgvector computes distance over an index rather than in Python.
    """
    return sum(a * b for a, b in zip(left, right, strict=True))
