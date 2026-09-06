"""Memory extraction: parsing, filtering, and prompt rendering.

The extractor's input is a language model's output, so it is untrusted in the
ordinary engineering sense -- it will be malformed, over-long, mis-scaled, and
occasionally conversational instead of JSON. These tests describe what happens
in each of those cases, because the requirement is that none of them damages
a conversation that has already been answered.
"""

from __future__ import annotations

import json
import uuid

import pytest

from nova.ai.base import ChatCompletion, ChatRequest, TokenUsage
from nova.ai.errors import AIUnavailableError
from nova.ai.lexical_embeddings import LexicalEmbeddingProvider, cosine_similarity
from nova.core.config import AISettings
from nova.models.memory import CATEGORIES, Memory
from nova.repositories.memory import ScoredMemory
from nova.services.memory import (
    MAX_MEMORIES_PER_EXCHANGE,
    MAX_MEMORY_LENGTH,
    MemoryExtractor,
    looks_sensitive,
    parse_candidates,
    render_context,
)


def _element(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "content": "Drinks coffee every morning",
        "category": "routine",
        "importance": 0.7,
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


class ScriptedProvider:
    """Returns whatever text the test wants, or raises."""

    def __init__(self, text: str = "[]", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.requests: list[ChatRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return "scripted-model"

    async def stream(self, request: ChatRequest):  # pragma: no cover - unused here
        yield self._text

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        if self._error is not None:
            raise self._error
        self.requests.append(request)
        return ChatCompletion(text=self._text, model=self.model, usage=TokenUsage())


class TestParsing:
    def test_parses_a_clean_array(self) -> None:
        candidates = parse_candidates(json.dumps([_element()]))

        assert len(candidates) == 1
        assert candidates[0].content == "Drinks coffee every morning"
        assert candidates[0].category == "routine"

    def test_survives_prose_around_the_array(self) -> None:
        # Models add a preamble however firmly they are told not to.
        raw = f"Sure, here is what I found:\n\n{json.dumps([_element()])}\n\nLet me know."
        assert len(parse_candidates(raw)) == 1

    def test_survives_a_code_fence(self) -> None:
        raw = f"```json\n{json.dumps([_element()])}\n```"
        assert len(parse_candidates(raw)) == 1

    def test_empty_array_is_a_valid_answer(self) -> None:
        # The common case, and it must not look like a failure.
        assert parse_candidates("[]") == []

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "I could not find anything worth remembering.",
            "[",
            "[{",
            '[{"content": "unterminated',
            "null",
            '{"content": "an object, not an array"}',
            # Bracketed but not JSON: single quotes, a trailing comma.
            "[{'content': 'python, not json'}]",
            "[1, 2,]",
        ],
    )
    def test_unusable_output_yields_nothing(self, raw: str) -> None:
        assert parse_candidates(raw) == []

    def test_rejects_non_text_content(self) -> None:
        assert parse_candidates(json.dumps([_element(content=42)])) == []

    def test_a_bad_element_does_not_lose_the_good_ones(self) -> None:
        """One unusable candidate should not cost the batch it came in."""
        raw = json.dumps(
            [
                _element(content="Has a dog called Pepper", category="relationship"),
                "not an object",
                _element(category="vibes"),  # invented category
                _element(content=""),
                _element(importance="high"),  # not a number
                _element(content="Works as an architect", category="fact"),
            ]
        )

        contents = [c.content for c in parse_candidates(raw)]
        assert contents == ["Has a dog called Pepper", "Works as an architect"]

    def test_rejects_content_longer_than_a_sentence(self) -> None:
        # A summary of the whole conversation would dominate every later
        # retrieval, so length is a rejection rather than a truncation.
        assert parse_candidates(json.dumps([_element(content="x" * (MAX_MEMORY_LENGTH + 1))])) == []

    def test_collapses_whitespace(self) -> None:
        candidates = parse_candidates(json.dumps([_element(content="  Runs\n\n  at   six  ")]))
        assert candidates[0].content == "Runs at six"

    def test_caps_how_many_come_from_one_exchange(self) -> None:
        raw = json.dumps([_element(content=f"Fact number {n}") for n in range(20)])
        assert len(parse_candidates(raw)) == MAX_MEMORIES_PER_EXCHANGE

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_accepts_every_known_category(self, category: str) -> None:
        assert parse_candidates(json.dumps([_element(category=category)]))

    def test_category_case_is_forgiven(self) -> None:
        candidates = parse_candidates(json.dumps([_element(category="ROUTINE")]))
        assert candidates[0].category == "routine"

    def test_out_of_range_scores_are_clamped(self) -> None:
        """A model confused about the scale is not confused about the memory."""
        candidates = parse_candidates(json.dumps([_element(importance=8, confidence=-2)]))

        assert candidates[0].importance == 1.0
        assert candidates[0].confidence == 0.0

    def test_booleans_are_not_scores(self) -> None:
        # ``True`` is an int in Python; accepting it would store 1.0 silently.
        assert parse_candidates(json.dumps([_element(importance=True)])) == []

    def test_missing_scores_are_rejected(self) -> None:
        element = _element()
        del element["confidence"]
        assert parse_candidates(json.dumps([element])) == []


class TestSensitiveContent:
    @pytest.mark.parametrize(
        "content",
        [
            "Their card number is 4111 1111 1111 1111",
            "Wifi password is hunter2correct",
            "API key: sk-ant-abcdefghijklmnop",
            "Uses the token = abc123def456",
            "Pasted a key aB3dEf7hIj9kLm2nOp5qRs8tUv1wXy4z",
        ],
    )
    def test_rejects_things_that_should_never_be_stored(self, content: str) -> None:
        assert looks_sensitive(content)
        assert parse_candidates(json.dumps([_element(content=content)])) == []

    @pytest.mark.parametrize(
        "content",
        [
            "Keeps forgetting their password",
            "Works on a token-based authentication system",
            "Their phone number ends in 4471",
            "Prefers long-running background jobs over polling",
            "Lives in a first-floor flat",
        ],
    )
    def test_keeps_ordinary_memories_that_mention_those_words(self, content: str) -> None:
        # A filter that ate these would quietly make NOVA worse at the exact
        # subject its owner works in.
        assert not looks_sensitive(content)


class TestExtractor:
    def _extractor(self, provider: ScriptedProvider, **settings: object) -> MemoryExtractor:
        return MemoryExtractor(provider=provider, settings=AISettings(**settings))  # type: ignore[arg-type]

    async def test_extracts_from_an_exchange(self) -> None:
        provider = ScriptedProvider(json.dumps([_element()]))
        candidates = await self._extractor(provider).extract(
            user_text="I have coffee every morning", assistant_text="Noted."
        )

        assert len(candidates) == 1
        # Both halves of the exchange reach the model: the reply can carry
        # the fact the question only implied.
        sent = provider.requests[0].messages[0].content
        assert "coffee every morning" in sent
        assert "Noted." in sent

    async def test_a_provider_failure_costs_nothing_but_the_memory(self) -> None:
        provider = ScriptedProvider(error=AIUnavailableError())
        assert await self._extractor(provider).extract(user_text="Hi", assistant_text="Hello") == []

    async def test_conversational_output_yields_nothing(self) -> None:
        """The offline provider does exactly this, and must not break."""
        provider = ScriptedProvider("I'm running without a language model at the moment.")
        assert await self._extractor(provider).extract(user_text="Hi", assistant_text="") == []

    async def test_drops_candidates_below_the_confidence_floor(self) -> None:
        provider = ScriptedProvider(
            json.dumps(
                [
                    _element(content="Might move to Berlin", confidence=0.2),
                    _element(content="Works as an architect", confidence=0.95),
                ]
            )
        )
        candidates = await self._extractor(provider, memory_min_confidence=0.5).extract(
            user_text="...", assistant_text="..."
        )

        assert [c.content for c in candidates] == ["Works as an architect"]

    async def test_blank_input_is_not_worth_a_model_call(self) -> None:
        provider = ScriptedProvider(json.dumps([_element()]))
        assert await self._extractor(provider).extract(user_text="   ", assistant_text="Hi") == []
        assert provider.requests == []


class TestContextRendering:
    def _scored(self, content: str, category: str = "fact") -> ScoredMemory:
        return ScoredMemory(
            memory=Memory(
                id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                content=content,
                category=category,
                importance=0.5,
                confidence=0.5,
                embedding=[0.0],
            ),
            distance=0.2,
        )

    def test_nothing_retrieved_renders_nothing(self) -> None:
        # An empty heading would tell the model NOVA remembers nothing,
        # which is a different claim from saying nothing at all.
        assert render_context([]) is None

    def test_includes_content_and_category(self) -> None:
        rendered = render_context([self._scored("Drinks coffee", "routine")])

        assert rendered is not None
        assert "Drinks coffee" in rendered
        assert "routine" in rendered

    def test_tells_the_model_not_to_recite_them(self) -> None:
        rendered = render_context([self._scored("Has a dog")])
        assert rendered is not None
        assert "do not list them back" in rendered.lower()


class TestLexicalEmbeddings:
    """The embedder has to actually retrieve, or memory is theatre."""

    async def test_related_text_scores_above_unrelated(self) -> None:
        provider = LexicalEmbeddingProvider(dimensions=1536)
        query, related, unrelated = await provider.embed(
            [
                "I drink coffee every morning before work",
                "The owner drinks coffee in the mornings",
                "The deployment pipeline failed on the staging cluster",
            ]
        )

        assert cosine_similarity(query, related) > 0.3
        assert cosine_similarity(query, related) > cosine_similarity(query, unrelated) + 0.25

    async def test_matches_across_word_endings(self) -> None:
        # Character n-grams are what make "runs" and "running" meet.
        provider = LexicalEmbeddingProvider(dimensions=1536)
        left, right = await provider.embed(["She is running late", "She runs late"])
        assert cosine_similarity(left, right) > 0.3

    async def test_vectors_are_unit_length(self) -> None:
        vector = (await LexicalEmbeddingProvider(dimensions=512).embed(["hello there"]))[0]
        assert sum(v * v for v in vector) ** 0.5 == pytest.approx(1.0, abs=1e-9)

    async def test_content_free_text_embeds_to_zeros(self) -> None:
        """pgvector treats an all-zero vector as maximally distant.

        That is the right answer for text with nothing in it, and it is why
        normalisation has to special-case a zero magnitude instead of
        dividing by it.
        """
        for text in ["", "   ", "the and of it"]:
            vector = (await LexicalEmbeddingProvider(dimensions=256).embed([text]))[0]
            assert not any(vector)
