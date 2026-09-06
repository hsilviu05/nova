"""Memory end to end: retrieval, isolation, extraction, and the routes.

The claim this phase makes is that NOVA remembers something said in one
conversation and uses it in another. That is what these prove, against a real
Postgres with pgvector and a real embedder -- not a stub that returns a
prearranged list.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from nova.ai.base import ChatCompletion, ChatRequest, TokenUsage
from nova.ai.lexical_embeddings import LexicalEmbeddingProvider
from nova.core.clock import utc_now
from nova.core.config import AISettings
from nova.models.memory import Memory
from nova.models.user import User
from nova.repositories.memory import MemoryRepository
from nova.services.memory import DEDUPLICATION_DISTANCE
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


class RememberingProvider:
    """Answers chat normally and returns fixed JSON for extraction.

    One provider serves both calls, so the two are told apart the way the
    real code does: by the system prompt. That keeps the test honest about
    the extractor actually being invoked on the extraction path.
    """

    def __init__(self, extraction: list[dict[str, Any]] | None = None) -> None:
        self._extraction = json.dumps(extraction or [])
        self.contexts: list[str | None] = []

    @property
    def name(self) -> str:
        return "remembering"

    @property
    def model(self) -> str:
        return "remembering-model"

    def _is_extraction(self, request: ChatRequest) -> bool:
        return request.system.startswith("You extract")

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        self.contexts.append(request.context)
        yield "Understood."

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        if self._is_extraction(request):
            return ChatCompletion(text=self._extraction, model=self.model, usage=TokenUsage())

        self.contexts.append(request.context)
        return ChatCompletion(text="Understood.", model=self.model, usage=TokenUsage())


def _memory(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "content": "Drinks coffee every morning before work",
        "category": "routine",
        "importance": 0.8,
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


async def _make_user(session: Any, email: str | None = None) -> User:
    user = User(
        email=email or f"memory-{uuid.uuid4().hex[:10]}@example.com",
        password_hash="not-a-real-hash",
        display_name="Memory Test",
    )
    session.add(user)
    await session.flush()
    return user


async def _store(
    repository: MemoryRepository,
    embedder: LexicalEmbeddingProvider,
    owner_id: uuid.UUID,
    content: str,
    *,
    category: str = "fact",
) -> Memory:
    embedding = (await embedder.embed([content]))[0]
    memory = Memory(
        user_id=owner_id,
        content=content,
        category=category,
        importance=0.5,
        confidence=0.8,
        embedding=embedding,
        embedding_provider=embedder.name,
    )
    repository.add(memory)
    await repository.session.flush()
    return memory


@pytest.fixture
def embedder() -> LexicalEmbeddingProvider:
    return LexicalEmbeddingProvider(dimensions=1536)


class TestVectorSearch:
    async def test_finds_the_related_memory_first(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        """Retrieval, through Postgres and pgvector, not in Python."""
        user = await _make_user(session)
        repository = MemoryRepository(session)

        await _store(
            repository, embedder, user.id, "Drinks coffee every morning", category="routine"
        )
        await _store(repository, embedder, user.id, "Deploys the staging cluster on Fridays")
        await _store(
            repository, embedder, user.id, "Has a cat called Pepper", category="relationship"
        )

        query = (await embedder.embed(["Do I usually have coffee in the morning?"]))[0]

        # With the cut-off in place only the relevant one survives, which is
        # the behaviour that matters.
        matches = await repository.search(user.id, query, limit=3, max_distance=0.85)
        assert [m.memory.content for m in matches] == ["Drinks coffee every morning"]

        # Without it, all three come back -- and the ordering has to be right,
        # or the cut-off would be doing the retrieval on its own.
        ranked = await repository.search(user.id, query, limit=3, max_distance=2.0)
        assert len(ranked) == 3
        assert "coffee" in ranked[0].memory.content
        assert ranked[0].similarity > ranked[1].similarity >= ranked[2].similarity

    async def test_unrelated_memories_are_left_out(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        """The distance cut-off is what stops the prompt filling with noise.

        Without it, a search always returns ``limit`` rows however unrelated,
        and NOVA reads as though it is confusing people.
        """
        user = await _make_user(session)
        repository = MemoryRepository(session)
        await _store(repository, embedder, user.id, "Deploys the staging cluster on Fridays")

        query = (await embedder.embed(["What sort of cheese do I like?"]))[0]
        assert await repository.search(user.id, query, limit=5, max_distance=0.85) == []

    async def test_one_persons_memories_never_reach_another(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        """The ownership boundary, tested with text that would otherwise match.

        This is the failure that matters most in this phase: retrieved text
        goes straight into a prompt, so a leak here is a leak into somebody
        else's conversation.
        """
        owner = await _make_user(session)
        stranger = await _make_user(session)
        repository = MemoryRepository(session)

        await _store(
            repository, embedder, owner.id, "Drinks coffee every morning", category="routine"
        )

        query = (await embedder.embed(["Do I drink coffee every morning?"]))[0]

        assert await repository.search(owner.id, query, limit=5, max_distance=0.85)
        assert await repository.search(stranger.id, query, limit=5, max_distance=0.85) == []

    async def test_a_zero_limit_makes_no_query(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        user = await _make_user(session)
        repository = MemoryRepository(session)
        query = (await embedder.embed(["anything"]))[0]

        assert await repository.search(user.id, query, limit=0, max_distance=1.0) == []

    async def test_recording_no_recalls_is_a_no_op(self, session: Any) -> None:
        # A search that matched nothing still calls this; it must not issue
        # an UPDATE with an empty IN clause.
        await MemoryRepository(session).record_recall([], at=utc_now())

    async def test_recall_is_recorded(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        user = await _make_user(session)
        repository = MemoryRepository(session)
        memory = await _store(repository, embedder, user.id, "Runs at six in the morning")

        await repository.record_recall([memory.id], at=utc_now())
        await session.flush()
        await session.refresh(memory)

        assert memory.recall_count == 1
        assert memory.last_recalled_at is not None


class TestDeduplication:
    async def test_the_same_fact_twice_finds_itself(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        user = await _make_user(session)
        repository = MemoryRepository(session)
        await _store(repository, embedder, user.id, "Drinks coffee every morning")

        again = (await embedder.embed(["Drinks coffee every morning"]))[0]
        assert (
            await repository.find_similar(user.id, again, threshold=DEDUPLICATION_DISTANCE)
            is not None
        )

    async def test_a_different_fact_does_not(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        user = await _make_user(session)
        repository = MemoryRepository(session)
        await _store(repository, embedder, user.id, "Drinks coffee every morning")

        other = (await embedder.embed(["Has a cat called Pepper"]))[0]
        assert (
            await repository.find_similar(user.id, other, threshold=DEDUPLICATION_DISTANCE) is None
        )


class TestBulkDeletion:
    async def test_clears_only_the_owners_memories(
        self, session: Any, embedder: LexicalEmbeddingProvider
    ) -> None:
        owner = await _make_user(session)
        stranger = await _make_user(session)
        repository = MemoryRepository(session)

        await _store(repository, embedder, owner.id, "Something about the owner")
        await _store(repository, embedder, owner.id, "Another thing about the owner")
        await _store(repository, embedder, stranger.id, "Something about somebody else")

        assert await repository.delete_for_owner(owner.id) == 2
        assert await repository.count_for_owner(owner.id) == 0
        assert await repository.count_for_owner(stranger.id) == 1


@pytest.fixture
async def remembering(
    settings: Any, engine: Any, session_factory: Any, redis_client: Any
) -> AsyncIterator[tuple[AsyncClient, RememberingProvider]]:
    """A signed-in client whose model extracts one memory per exchange."""
    provider = RememberingProvider([_memory()])
    app = build_test_app(
        settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis_client,
        chat_provider=provider,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://nova.test") as client:
        yield client, provider


async def _sign_in(client: AsyncClient) -> dict[str, str]:
    body = (
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"user-{uuid.uuid4().hex[:10]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Memory",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


async def _say(client: AsyncClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    conversation = (await client.post("/api/v1/conversations", headers=headers, json={})).json()
    response = await client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"content": text},
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestRememberingAcrossConversations:
    async def test_a_fact_from_one_thread_reaches_the_next(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        """The whole point of the phase, in one test.

        Say something in one conversation; the memory is extracted after the
        reply. Ask about it in a *different* conversation -- where the message
        history shares nothing -- and it has to arrive in the context.
        """
        client, provider = remembering
        headers = await _sign_in(client)

        await _say(client, headers, "I have coffee every morning before work")

        stored = (await client.get("/api/v1/memories", headers=headers)).json()
        assert stored["total"] == 1
        assert "coffee" in stored["items"][0]["content"]

        provider.contexts.clear()
        await _say(client, headers, "Do I usually drink coffee in the morning?")

        context = provider.contexts[0]
        assert context is not None
        assert "Drinks coffee every morning before work" in context

    async def test_nothing_remembered_means_no_context(
        self, settings: Any, engine: Any, session_factory: Any, redis_client: Any
    ) -> None:
        provider = RememberingProvider([])
        app = build_test_app(
            settings,
            engine=engine,
            session_factory=session_factory,
            redis=redis_client,
            chat_provider=provider,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.test"
        ) as client:
            headers = await _sign_in(client)
            await _say(client, headers, "Nothing much to report")

            assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 0
            assert provider.contexts == [None]

    async def test_the_same_fact_twice_is_stored_once(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)

        await _say(client, headers, "I have coffee every morning")
        await _say(client, headers, "Coffee, every single morning")

        assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 1

    async def test_streaming_extracts_too(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)
        conversation = (await client.post("/api/v1/conversations", headers=headers, json={})).json()

        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=headers,
            json={"content": "I have coffee every morning"},
        )
        assert response.status_code == 200

        assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 1

    async def test_memories_are_scoped_to_the_account(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        owner = await _sign_in(client)
        stranger = await _sign_in(client)

        await _say(client, owner, "I have coffee every morning")

        assert (await client.get("/api/v1/memories", headers=stranger)).json()["total"] == 0


class TestMemoryRoutes:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/memories")).status_code == 401

    async def test_lists_and_filters_by_category(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)

        await _say(client, headers, "I have coffee every morning")

        by_category = await client.get(
            "/api/v1/memories", headers=headers, params={"category": "routine"}
        )
        assert by_category.json()["total"] == 1

        other = await client.get(
            "/api/v1/memories", headers=headers, params={"category": "project"}
        )
        assert other.json()["total"] == 0

    async def test_rejects_an_unknown_category(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)

        response = await client.get(
            "/api/v1/memories", headers=headers, params={"category": "vibes"}
        )
        assert response.status_code == 422

    async def test_search_exposes_similarity(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)
        await _say(client, headers, "I have coffee every morning")

        results = (
            await client.get(
                "/api/v1/memories/search", headers=headers, params={"q": "coffee in the morning"}
            )
        ).json()

        assert results
        assert results[0]["similarity"] > 0.0
        assert "coffee" in results[0]["memory"]["content"]

    async def test_editing_a_memory_changes_what_retrieves_it(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        """A correction has to reach the embedding, not just the screen.

        Skipping the re-embed would leave a memory that reads correctly and
        is still found by whatever it used to say -- the worst of both.
        """
        client, _ = remembering
        headers = await _sign_in(client)
        await _say(client, headers, "I have coffee every morning")

        memory_id = (await client.get("/api/v1/memories", headers=headers)).json()["items"][0]["id"]
        updated = await client.patch(
            f"/api/v1/memories/{memory_id}",
            headers=headers,
            json={"content": "Drinks peppermint tea in the evening", "category": "preference"},
        )
        assert updated.status_code == 200
        assert updated.json()["category"] == "preference"
        # A person correcting NOVA is the strongest evidence there is.
        assert updated.json()["confidence"] == 1.0

        found = (
            await client.get(
                "/api/v1/memories/search", headers=headers, params={"q": "peppermint tea"}
            )
        ).json()
        assert found and "peppermint" in found[0]["memory"]["content"]

        stale = (
            await client.get(
                "/api/v1/memories/search", headers=headers, params={"q": "coffee every morning"}
            )
        ).json()
        assert stale == []

    async def test_reweighting_leaves_the_text_alone(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        """Only what was sent changes; an omitted field is not a clear."""
        client, _ = remembering
        headers = await _sign_in(client)
        await _say(client, headers, "I have coffee every morning")
        original = (await client.get("/api/v1/memories", headers=headers)).json()["items"][0]

        updated = await client.patch(
            f"/api/v1/memories/{original['id']}",
            headers=headers,
            # An explicit null means "leave it", the same as omitting it --
            # never "blank the memory out".
            json={"content": None, "importance": 0.1},
        )

        assert updated.status_code == 200
        assert updated.json()["importance"] == 0.1
        assert updated.json()["content"] == original["content"]
        assert updated.json()["category"] == original["category"]
        # Untouched text keeps the extractor's confidence rather than being
        # promoted to a human correction.
        assert updated.json()["confidence"] == original["confidence"]

    async def test_rejects_a_blank_correction(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)
        await _say(client, headers, "I have coffee every morning")
        memory_id = (await client.get("/api/v1/memories", headers=headers)).json()["items"][0]["id"]

        response = await client.patch(
            f"/api/v1/memories/{memory_id}", headers=headers, json={"content": "   "}
        )
        assert response.status_code == 422

    async def test_deletes_one_memory(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)
        await _say(client, headers, "I have coffee every morning")
        memory_id = (await client.get("/api/v1/memories", headers=headers)).json()["items"][0]["id"]

        assert (
            await client.delete(f"/api/v1/memories/{memory_id}", headers=headers)
        ).status_code == 204
        assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 0

    async def test_cannot_touch_someone_elses_memory(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        """Not found rather than forbidden: existence is not confirmed."""
        client, _ = remembering
        owner = await _sign_in(client)
        stranger = await _sign_in(client)

        await _say(client, owner, "I have coffee every morning")
        memory_id = (await client.get("/api/v1/memories", headers=owner)).json()["items"][0]["id"]

        assert (
            await client.patch(
                f"/api/v1/memories/{memory_id}", headers=stranger, json={"importance": 0.1}
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/v1/memories/{memory_id}", headers=stranger)
        ).status_code == 404
        # And it is still there for the person it belongs to.
        assert (await client.get("/api/v1/memories", headers=owner)).json()["total"] == 1

    async def test_forgets_everything_on_request(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)
        await _say(client, headers, "I have coffee every morning")

        assert (await client.delete("/api/v1/memories", headers=headers)).status_code == 204
        assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 0

    async def test_a_missing_memory_is_a_404(
        self, remembering: tuple[AsyncClient, RememberingProvider]
    ) -> None:
        client, _ = remembering
        headers = await _sign_in(client)

        response = await client.delete(f"/api/v1/memories/{uuid.uuid4()}", headers=headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "memory_not_found"


class TestExtractionSettings:
    async def test_extraction_can_be_turned_off(
        self, settings: Any, engine: Any, session_factory: Any, redis_client: Any
    ) -> None:
        """It costs a model call per exchange, so it has to be optional."""
        without = settings.model_copy(deep=True)
        without.ai.memory_extraction_enabled = False

        app = build_test_app(
            without,
            engine=engine,
            session_factory=session_factory,
            redis=redis_client,
            chat_provider=RememberingProvider([_memory()]),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.test"
        ) as client:
            headers = await _sign_in(client)
            await _say(client, headers, "I have coffee every morning")

            assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 0

    async def test_sensitive_content_never_reaches_the_database(
        self, settings: Any, engine: Any, session_factory: Any, redis_client: Any
    ) -> None:
        """The model is told not to extract secrets. This is when it does.

        End to end rather than at the parser, because the property that
        matters is that no row exists -- not that one function returned
        ``None`` on the way there.
        """
        app = build_test_app(
            settings,
            engine=engine,
            session_factory=session_factory,
            redis=redis_client,
            chat_provider=RememberingProvider(
                [
                    _memory(content="Their wifi password is correcthorse", category="fact"),
                    _memory(content="Works as an architect", category="fact"),
                ]
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.test"
        ) as client:
            headers = await _sign_in(client)
            await _say(client, headers, "Setting up the wifi")

            body = (await client.get("/api/v1/memories", headers=headers)).json()
            contents = [item["content"] for item in body["items"]]

            assert contents == ["Works as an architect"]

    async def test_retrieval_can_be_turned_off(
        self, settings: Any, engine: Any, session_factory: Any, redis_client: Any
    ) -> None:
        muted = settings.model_copy(deep=True)
        muted.ai.memory_retrieval_limit = 0

        provider = RememberingProvider([_memory()])
        app = build_test_app(
            muted,
            engine=engine,
            session_factory=session_factory,
            redis=redis_client,
            chat_provider=provider,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.test"
        ) as client:
            headers = await _sign_in(client)
            await _say(client, headers, "I have coffee every morning")

            provider.contexts.clear()
            await _say(client, headers, "Do I drink coffee?")

            assert provider.contexts == [None]


class TestStoredShape:
    async def test_the_source_conversation_is_recorded(
        self, remembering: tuple[AsyncClient, RememberingProvider], session: Any
    ) -> None:
        """So "why does it think that?" has an answer."""
        client, _ = remembering
        headers = await _sign_in(client)
        exchange = await _say(client, headers, "I have coffee every morning")

        stored = (await session.execute(select(Memory))).scalars().all()
        assert len(stored) == 1
        assert stored[0].source_conversation_id is not None
        assert stored[0].embedding_provider == "lexical"
        assert exchange["assistant_message"]["content"]

    async def test_deleting_a_conversation_keeps_what_was_learned(
        self, remembering: tuple[AsyncClient, RememberingProvider], session: Any
    ) -> None:
        """SET NULL, not CASCADE.

        Deleting a thread should tidy the transcript, not erase everything
        NOVA understood from it -- those are different intentions.
        """
        client, _ = remembering
        headers = await _sign_in(client)

        conversation = (await client.post("/api/v1/conversations", headers=headers, json={})).json()
        await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"content": "I have coffee every morning"},
        )
        await client.delete(f"/api/v1/conversations/{conversation['id']}", headers=headers)

        body = (await client.get("/api/v1/memories", headers=headers)).json()
        assert body["total"] == 1
        assert body["items"][0]["source_conversation_id"] is None


class TestDegradedRetrieval:
    async def test_a_broken_embedder_costs_memory_not_the_reply(
        self, settings: Any, engine: Any, session_factory: Any, redis_client: Any
    ) -> None:
        """Continuity is worth less than an answer.

        If the embedder fails, NOVA should reply without its memories rather
        than return an error for a question it could have answered.
        """

        class BrokenEmbedder:
            @property
            def name(self) -> str:
                return "broken"

            @property
            def dimensions(self) -> int:
                return 1536

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embedding backend is down")

        app = build_test_app(
            settings,
            engine=engine,
            session_factory=session_factory,
            redis=redis_client,
            chat_provider=RememberingProvider([_memory()]),
            embedding_provider=BrokenEmbedder(),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.test"
        ) as client:
            headers = await _sign_in(client)
            exchange = await _say(client, headers, "Are you still there?")

            assert exchange["assistant_message"]["content"]
            # And nothing was stored, because storing needs an embedding.
            assert (await client.get("/api/v1/memories", headers=headers)).json()["total"] == 0


class TestSettingsGuard:
    def test_the_retrieval_defaults_are_sane(self) -> None:
        ai = AISettings()
        assert 0 < ai.memory_retrieval_limit <= 20
        assert 0 < ai.memory_max_distance <= 1.0
