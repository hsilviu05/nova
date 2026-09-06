"""A provider that needs no network and no credential.

Two jobs, both real:

* the test suite runs the whole conversation path without an API key, so CI
  needs no secret and tests cost nothing and never flake on a rate limit;
* NOVA still answers when the AI provider is unconfigured or unreachable,
  which matters for a device whose whole premise is that it keeps behaving
  when the network does not.

Replies are deterministic and openly canned. It does not pretend to be a
language model, because a fake that looked real would eventually be mistaken
for one in a demo.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from nova.ai.base import ChatCompletion, ChatRequest, TokenUsage

# Chosen to sound like NOVA -- concise, observant, faintly dry -- while
# being obviously canned on a second reading.
_REPLIES: tuple[str, ...] = (
    "I'm running without a language model at the moment, so this is all I have.",
    "Noted. I can't think properly right now, but I heard you.",
    "My reasoning is offline. Ask me again when I'm fully awake.",
    "I'd give you a better answer with a model attached.",
    "Still here. Still not especially clever without a provider configured.",
)

_EMPTY_INPUT_REPLY = "You didn't say anything. I noticed that too."


class OfflineChatProvider:
    """Deterministic replies, no network.

    The reply is chosen by hashing the last user message, so the same input
    always produces the same output -- a test can assert on it, and a
    conversation does not repeat itself turn after turn.
    """

    def __init__(self, *, chunk_size: int = 12) -> None:
        # Small chunks so streaming behaviour is exercised rather than the
        # whole reply arriving as one event.
        self._chunk_size = max(1, chunk_size)

    @property
    def name(self) -> str:
        return "offline"

    @property
    def model(self) -> str:
        return "offline-deterministic"

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        text = self._reply_for(request)
        for index in range(0, len(text), self._chunk_size):
            yield text[index : index + self._chunk_size]

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        text = self._reply_for(request)
        return ChatCompletion(
            text=text,
            model=self.model,
            usage=TokenUsage(
                # Deliberately crude: a word count, not a real tokenizer.
                # Nothing should be charged or forecast from these numbers.
                input_tokens=sum(len(m.content.split()) for m in request.messages),
                output_tokens=len(text.split()),
            ),
            stop_reason="end_turn",
        )

    def _reply_for(self, request: ChatRequest) -> str:
        last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        if not last_user.strip():
            return _EMPTY_INPUT_REPLY

        digest = hashlib.sha256(last_user.encode("utf-8")).digest()
        return _REPLIES[digest[0] % len(_REPLIES)]


class OfflineEmbeddingProvider:
    """Deterministic pseudo-embeddings, no network.

    Hashed rather than learned, so vectors are stable and unit-length but
    carry no semantics: two paraphrases are as far apart as two unrelated
    sentences. Adequate for exercising storage, indexing and retrieval
    plumbing in Phase 5; useless for judging retrieval quality, which needs a
    real provider.
    """

    def __init__(self, *, dimensions: int = 1536) -> None:
        self._dimensions = dimensions

    @property
    def name(self) -> str:
        return "offline"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        raw = (seed * (self._dimensions // len(seed) + 1))[: self._dimensions]

        # Centre on zero, then normalise, so vectors are comparable by
        # cosine distance the way real embeddings are.
        centred = [(byte - 127.5) / 127.5 for byte in raw]
        magnitude = sum(value * value for value in centred) ** 0.5 or 1.0
        return [value / magnitude for value in centred]
