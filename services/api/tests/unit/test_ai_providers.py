"""The AI provider abstraction.

No network and no API key: the offline provider is a first-class
implementation, not a mock, so these exercise the real code path.
"""

from __future__ import annotations

import pytest

from nova.ai.base import ChatMessage, ChatProvider, ChatRequest, EmbeddingProvider
from nova.ai.errors import AIConfigurationError
from nova.ai.offline import OfflineChatProvider, OfflineEmbeddingProvider
from nova.ai.registry import build_chat_provider, build_embedding_provider
from nova.core.config import AISettings
from nova.models.memory import EMBEDDING_DIMENSIONS
from nova.services.persona import Persona, build_system_prompt


def _request(text: str = "Hello") -> ChatRequest:
    return ChatRequest(system="You are NOVA.", messages=[ChatMessage(role="user", content=text)])


class TestProtocolConformance:
    def test_offline_chat_satisfies_the_protocol(self) -> None:
        # The protocol is what business logic depends on; an implementation
        # that drifts from it should fail here, not at a call site.
        assert isinstance(OfflineChatProvider(), ChatProvider)

    def test_offline_embeddings_satisfy_the_protocol(self) -> None:
        assert isinstance(OfflineEmbeddingProvider(), EmbeddingProvider)


class TestOfflineChat:
    async def test_completes(self) -> None:
        completion = await OfflineChatProvider().complete(_request())

        assert completion.text
        assert completion.model == "offline-deterministic"
        assert completion.stop_reason == "end_turn"

    async def test_streams_in_several_chunks(self) -> None:
        provider = OfflineChatProvider(chunk_size=4)
        chunks = [chunk async for chunk in provider.stream(_request())]

        # Streaming behaviour has to be exercised, not just the final text.
        assert len(chunks) > 1
        assert "".join(chunks) == (await provider.complete(_request())).text

    async def test_is_deterministic(self) -> None:
        provider = OfflineChatProvider()
        first = await provider.complete(_request("Same question"))
        second = await provider.complete(_request("Same question"))

        assert first.text == second.text

    async def test_different_inputs_can_differ(self) -> None:
        provider = OfflineChatProvider()
        replies = {(await provider.complete(_request(f"question {n}"))).text for n in range(40)}
        # A single canned reply for everything would make conversation tests
        # pass without proving anything about context handling.
        assert len(replies) > 1

    async def test_handles_a_blank_message(self) -> None:
        completion = await OfflineChatProvider().complete(_request("   "))
        assert completion.text

    async def test_handles_no_user_message(self) -> None:
        request = ChatRequest(
            system="You are NOVA.",
            messages=[ChatMessage(role="assistant", content="Hello")],
        )
        assert (await OfflineChatProvider().complete(request)).text

    async def test_reports_usage(self) -> None:
        completion = await OfflineChatProvider().complete(_request("one two three four"))
        assert completion.usage.input_tokens > 0
        assert completion.usage.output_tokens > 0


class TestOfflineEmbeddings:
    async def test_returns_one_vector_per_text(self) -> None:
        vectors = await OfflineEmbeddingProvider(dimensions=64).embed(["a", "b", "c"])
        assert len(vectors) == 3

    async def test_vectors_have_the_configured_width(self) -> None:
        # pgvector columns are fixed-width; a mismatch is an insert error.
        vectors = await OfflineEmbeddingProvider(dimensions=128).embed(["hello"])
        assert len(vectors[0]) == 128

    async def test_vectors_are_unit_length(self) -> None:
        # Cosine distance assumes normalised vectors.
        vector = (await OfflineEmbeddingProvider(dimensions=256).embed(["hello"]))[0]
        magnitude = sum(value * value for value in vector) ** 0.5
        assert magnitude == pytest.approx(1.0, abs=1e-9)

    async def test_is_deterministic(self) -> None:
        provider = OfflineEmbeddingProvider(dimensions=64)
        first = await provider.embed(["stable"])
        second = await provider.embed(["stable"])
        assert first == second

    async def test_different_texts_differ(self) -> None:
        provider = OfflineEmbeddingProvider(dimensions=64)
        vectors = await provider.embed(["alpha", "beta"])
        assert vectors[0] != vectors[1]

    async def test_handles_an_empty_batch(self) -> None:
        assert await OfflineEmbeddingProvider().embed([]) == []


class TestRegistry:
    def test_offline_is_the_default(self) -> None:
        # So the stack starts and the suite runs without a key.
        assert build_chat_provider(AISettings()).name == "offline"

    def test_anthropic_without_a_key_degrades_rather_than_failing(self) -> None:
        """A companion that will not boot is worse than one that is honest."""
        provider = build_chat_provider(AISettings(chat_provider="anthropic"))
        assert provider.name == "offline"

    def test_anthropic_with_a_key_builds_the_real_adapter(self) -> None:
        provider = build_chat_provider(
            AISettings(
                chat_provider="anthropic",
                anthropic_api_key="sk-ant-not-a-real-key",  # type: ignore[arg-type]
            )
        )
        assert provider.name == "anthropic"
        assert provider.model == "claude-opus-5"

    def test_rejects_an_unknown_provider(self) -> None:
        settings = AISettings()
        # Bypass the Literal to prove the guard exists rather than trusting
        # validation to be the only defence.
        object.__setattr__(settings, "chat_provider", "telepathy")

        with pytest.raises(AIConfigurationError):
            build_chat_provider(settings)

    def test_builds_a_lexical_embedder_by_default(self) -> None:
        provider = build_embedding_provider(AISettings())
        assert provider.name == "lexical"
        assert provider.dimensions == EMBEDDING_DIMENSIONS

    def test_builds_the_hash_embedder_when_asked(self) -> None:
        provider = build_embedding_provider(AISettings(embedding_provider="hash"))
        assert provider.name == "offline"

    def test_rejects_a_width_the_column_cannot_hold(self) -> None:
        """A mismatch has to fail at startup, not on the first insert.

        Vectors of different widths cannot be compared at all, so a silent
        acceptance here would mean every future search returning nothing --
        or an error from Postgres per request, which is worse.
        """
        with pytest.raises(AIConfigurationError) as caught:
            build_embedding_provider(AISettings(embedding_dimensions=512))

        assert caught.value.code == "ai_embedding_dimension_mismatch"


class TestPersona:
    def test_prompt_names_nova_and_its_traits(self) -> None:
        prompt = build_system_prompt()

        assert "NOVA" in prompt
        assert "curious" in prompt

    def test_prompt_forbids_markdown(self) -> None:
        # Replies are spoken aloud or shown in a chat bubble; headings and
        # bullet points read as noise in both.
        assert "markdown" in build_system_prompt().lower()

    def test_prompt_is_stable(self) -> None:
        # Byte-stability is what lets the adapter mark it as a cacheable
        # prefix; anything varying per turn would break the cache each time.
        assert build_system_prompt() == build_system_prompt()

    def test_custom_instructions_are_included(self) -> None:
        prompt = build_system_prompt(Persona(custom_instructions="Never mention the weather."))
        assert "Never mention the weather." in prompt

    def test_context_notes_are_included(self) -> None:
        prompt = build_system_prompt(Persona(context_notes=("The owner is called Silviu.",)))
        assert "Silviu" in prompt

    def test_a_custom_name_is_used(self) -> None:
        assert "ARIA" in build_system_prompt(Persona(name="ARIA"))
