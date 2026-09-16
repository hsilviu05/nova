"""AI provider interfaces.

Business logic depends on these, never on a vendor SDK or on a particular
local runtime, per :doc:`ADR 002 <../../../docs/decisions/002-ai-provider-abstraction>`.
Interfaces are segregated by capability rather than combined into one fat
``AIProvider``: a local embedding server implements embeddings and nothing
else, and forcing it to satisfy a combined interface would mean stub methods
that raise.

Streaming yields *events* rather than strings. A reply is no longer only
text: the model may ask to run a tool partway through, and a consumer has to
be able to tell "NOVA said this" from "NOVA wants to do this". Flattening
both into a string would put the difference back in the parser, where a
malformed reply could forge a tool call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool the model asked to run.

    ``id`` correlates the call with its result across a round trip. Providers
    that do not supply one get a generated id, because the correlation is
    NOVA's requirement rather than theirs.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What running a tool produced, on its way back to the model.

    ``content`` is already-rendered text. It is data, never instruction --
    see :func:`nova.tools.safety.wrap_tool_output`, which is what actually
    frames it before it reaches a prompt.
    """

    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of a conversation, in provider-neutral form.

    An assistant turn may carry ``tool_calls`` instead of, or alongside,
    text. ``tool_results`` belongs to the *user* turn that answers them,
    which is how both the Anthropic and OpenAI wire formats model it.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool as the model sees it.

    Deliberately not the same object as :class:`nova.tools.base.Tool`: the
    model gets a name, a description, and a JSON Schema, and never learns
    what the tool does on the machine or what permission it carries.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """What the conversation service asks a provider for.

    ``system`` is separate from ``messages`` because every provider worth
    supporting treats it separately, and because keeping it stable is what
    makes prompt caching possible.

    ``context`` is the volatile half of the system prompt -- retrieved
    memories, the current system state. It is a separate field precisely so
    it cannot be concatenated into ``system``: a prefix that changes every
    turn is a prefix that never caches.
    """

    system: str
    messages: list[ChatMessage]
    context: str | None = None
    max_tokens: int = 1024
    tools: tuple[ToolDefinition, ...] = ()
    # Terminal answers are short and want low latency, so the default leans
    # cheap rather than thorough.
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
    tool_calls: tuple[ToolCall, ...] = ()
    # "end_turn", "max_tokens", "tool_use", "refusal", ... Kept as a plain
    # string so a new provider value does not require a schema change here.
    stop_reason: str | None = None

    @property
    def was_refused(self) -> bool:
        return self.stop_reason == "refusal"


# -- stream events ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A fragment of the visible reply."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallRequested:
    """The model has finished asking for a tool.

    Emitted only once the call's arguments are complete. A partially
    accumulated call is never surfaced: half a JSON object is not a request,
    and a consumer that saw one could act on arguments the model had not
    finished writing.
    """

    call: ToolCall


@dataclass(frozen=True, slots=True)
class StreamCompleted:
    """The provider finished this turn."""

    stop_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str | None = None


StreamEvent = TextDelta | ToolCallRequested | StreamCompleted


@runtime_checkable
class ChatProvider(Protocol):
    """Generates conversational replies."""

    @property
    def name(self) -> str:
        """Identifies the provider in logs and stored messages."""
        ...

    @property
    def model(self) -> str: ...

    @property
    def supports_tools(self) -> bool:
        """Whether this provider can be given tool definitions.

        Asked rather than assumed: handing tools to a provider that ignores
        them produces a model that describes running a command instead of
        running it, which reads as NOVA lying about what it did.
        """
        ...

    def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent, None]:
        """Yield reply events as they are produced.

        Streaming is the primary interface because the chat UI shows tokens
        as they arrive, and because a long reply on a non-streaming call can
        exceed an HTTP timeout.

        An async *generator*, not merely an iterator: when the consumer goes
        away mid-reply the caller closes it with ``aclosing``, and a provider
        holding a connection open releases it in that close rather than
        whenever the garbage collector gets to it.
        """
        ...

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        """Return a whole reply at once.

        Used where there is no one waiting on the tokens -- memory
        extraction, summarisation, background classification.
        """
        ...

    async def aclose(self) -> None:
        """Release any long-lived connection this provider holds."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors for semantic retrieval."""

    @property
    def name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...
