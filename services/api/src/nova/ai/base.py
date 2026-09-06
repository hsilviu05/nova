"""AI provider interfaces.

Business logic depends on these, never on a vendor SDK, per
:doc:`ADR 002 <../../../docs/decisions/002-ai-provider-abstraction>`.
Interfaces are segregated by capability rather than combined into one fat
``AIProvider``: a local Whisper deployment implements speech-to-text and
nothing else, and forcing it to satisfy a combined interface would mean stub
methods that raise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of a conversation, in provider-neutral form."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """What the conversation service asks a provider for.

    ``system`` is separate from ``messages`` because every provider worth
    supporting treats it separately, and because keeping it stable is what
    makes prompt caching possible.

    ``context`` is the volatile half of the system prompt -- retrieved
    memories, and later the device's state. It is a separate field precisely
    so it cannot be concatenated into ``system``: a prefix that changes every
    turn is a prefix that never caches.
    """

    system: str
    messages: list[ChatMessage]
    context: str | None = None
    max_tokens: int = 1024
    # Chat replies from a desk companion are short and want low latency, so
    # the default leans cheap rather than thorough.
    effort: Literal["low", "medium", "high"] = "low"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What a completion cost, for analytics and cost tracking."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """A finished reply."""

    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    # "end_turn", "max_tokens", "refusal", ... Kept as a plain string so a
    # new provider value does not require a schema change here.
    stop_reason: str | None = None

    @property
    def was_refused(self) -> bool:
        return self.stop_reason == "refusal"


@runtime_checkable
class ChatProvider(Protocol):
    """Generates conversational replies."""

    @property
    def name(self) -> str:
        """Identifies the provider in telemetry and stored messages."""
        ...

    @property
    def model(self) -> str: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """Yield reply text as it is produced.

        Streaming is the primary interface because the chat UI shows tokens
        as they arrive, and because a long reply on a non-streaming call can
        exceed an HTTP timeout.
        """
        ...

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        """Return a whole reply at once.

        Used where there is no one waiting on the tokens -- memory
        extraction, summarisation, background classification.
        """
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors for semantic retrieval.

    Defined here in Phase 4 although Phase 5 is the first user: the point of
    ADR 002 is that the interface exists before any business logic could
    accidentally reach past it to a vendor SDK.
    """

    @property
    def name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
