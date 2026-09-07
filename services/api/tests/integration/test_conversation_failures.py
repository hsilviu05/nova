"""What happens when the AI provider misbehaves.

The offline provider always succeeds, so these substitute providers that fail
in specific ways. These paths matter more than the happy one: a companion
whose model is rate-limited or which declines a question must still leave the
conversation in a coherent state.
"""

from __future__ import annotations

import asyncio
import gc
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from nova.ai.base import ChatCompletion, ChatRequest, TokenUsage
from nova.ai.errors import AIRefusalError, AIUnavailableError
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


class FailingProvider:
    """Fails before producing anything."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    @property
    def name(self) -> str:
        return "failing"

    @property
    def model(self) -> str:
        return "failing-model"

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        raise self._error
        yield ""  # pragma: no cover - unreachable, keeps this a generator

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        raise self._error


class TruncatingProvider:
    """Produces some text, then fails.

    The awkward case: the response is already streaming, so the failure
    cannot become an error status.
    """

    def __init__(self, text: str = "I was about to say") -> None:
        self._text = text

    @property
    def name(self) -> str:
        return "truncating"

    @property
    def model(self) -> str:
        return "truncating-model"

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        for word in self._text.split():
            yield word + " "
        raise AIUnavailableError()

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        return ChatCompletion(
            text=self._text, model=self.model, usage=TokenUsage(), stop_reason="end_turn"
        )


class ClosureRecordingProvider:
    """Streams a few words and records which task closed the stream.

    A generator closed by its consumer runs its ``finally`` in the consumer's
    task. One abandoned to the garbage collector runs it in the finalizer
    task asyncio schedules instead -- often within the same tick, so the
    frame is gone either way by the time a test can look. The task is the
    tell.
    """

    def __init__(self) -> None:
        self.closed_in: asyncio.Task[Any] | None = None

    @property
    def name(self) -> str:
        return "recording"

    @property
    def model(self) -> str:
        return "recording-model"

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        try:
            for word in ("I", "was", "about", "to", "say"):
                yield word + " "
        finally:
            self.closed_in = asyncio.current_task()

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        return ChatCompletion(
            text="I was about to say", model=self.model, usage=TokenUsage(), stop_reason="end_turn"
        )


def _client_for(provider: Any, settings, engine, session_factory, redis_client):
    app = build_test_app(
        settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis_client,
        chat_provider=provider,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://nova.test")


async def _signed_in(client: AsyncClient) -> dict[str, str]:
    body = (
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"fail-{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Tester",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


class TestProviderFailures:
    @pytest.mark.parametrize(
        ("error", "expected_status"),
        [
            (AIUnavailableError(), 503),
            (AIRefusalError(), 422),
        ],
    )
    async def test_non_streaming_surfaces_the_failure(
        self,
        settings,
        engine,
        session_factory,
        redis_client,
        error: Exception,
        expected_status: int,
    ) -> None:
        async with _client_for(
            FailingProvider(error), settings, engine, session_factory, redis_client
        ) as client:
            headers = await _signed_in(client)
            conversation = (
                await client.post("/api/v1/conversations", headers=headers, json={})
            ).json()

            response = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                headers=headers,
                json={"content": "Hello"},
            )

        assert response.status_code == expected_status

    async def test_a_refusal_is_distinguished_from_a_fault(
        self, settings, engine, session_factory, redis_client
    ) -> None:
        """Declining to answer is not the same as being broken."""
        async with _client_for(
            FailingProvider(AIRefusalError()),
            settings,
            engine,
            session_factory,
            redis_client,
        ) as client:
            headers = await _signed_in(client)
            conversation = (
                await client.post("/api/v1/conversations", headers=headers, json={})
            ).json()

            response = await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                headers=headers,
                json={"content": "Hello"},
            )

        assert response.json()["error"]["code"] == "ai_refused"

    async def test_the_user_turn_survives_a_failed_reply(
        self, settings, engine, session_factory, redis_client
    ) -> None:
        """Losing what the user said because NOVA could not answer is worse
        than showing a question with no answer."""
        async with _client_for(
            FailingProvider(AIUnavailableError()),
            settings,
            engine,
            session_factory,
            redis_client,
        ) as client:
            headers = await _signed_in(client)
            conversation = (
                await client.post("/api/v1/conversations", headers=headers, json={})
            ).json()

            await client.post(
                f"/api/v1/conversations/{conversation['id']}/stream",
                headers=headers,
                json={"content": "Remember this"},
            )

            detail = (
                await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
            ).json()

        contents = [m["content"] for m in detail["messages"]]
        assert "Remember this" in contents

    async def test_streaming_reports_a_failure_as_an_error_event(
        self, settings, engine, session_factory, redis_client
    ) -> None:
        # Once headers are sent the status is fixed at 200, so a failure
        # before any text has to arrive in-band.
        async with _client_for(
            FailingProvider(AIUnavailableError()),
            settings,
            engine,
            session_factory,
            redis_client,
        ) as client:
            headers = await _signed_in(client)
            conversation = (
                await client.post("/api/v1/conversations", headers=headers, json={})
            ).json()

            response = await client.post(
                f"/api/v1/conversations/{conversation['id']}/stream",
                headers=headers,
                json={"content": "Hello"},
            )

        assert response.status_code == 200
        assert "event: error" in response.text
        assert "ai_unavailable" in response.text
        assert "event: done" not in response.text


class TestPartialReplies:
    async def test_a_truncated_reply_is_still_stored(
        self, settings, engine, session_factory, redis_client
    ) -> None:
        """Whatever NOVA managed to say is what it said.

        Discarding it would leave the thread showing a question with no
        answer, even though the user watched words appear.
        """
        async with _client_for(
            TruncatingProvider(), settings, engine, session_factory, redis_client
        ) as client:
            headers = await _signed_in(client)
            conversation = (
                await client.post("/api/v1/conversations", headers=headers, json={})
            ).json()

            response = await client.post(
                f"/api/v1/conversations/{conversation['id']}/stream",
                headers=headers,
                json={"content": "Go on"},
            )

            detail = (
                await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
            ).json()

        # Text was delivered before the failure.
        assert "event: delta" in response.text

        assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"].strip() == "I was about to say"

    async def test_a_truncated_reply_counts_toward_the_conversation(
        self, settings, engine, session_factory, redis_client
    ) -> None:
        async with _client_for(
            TruncatingProvider(), settings, engine, session_factory, redis_client
        ) as client:
            headers = await _signed_in(client)
            conversation = (
                await client.post("/api/v1/conversations", headers=headers, json={})
            ).json()

            await client.post(
                f"/api/v1/conversations/{conversation['id']}/stream",
                headers=headers,
                json={"content": "Go on"},
            )

            detail = (
                await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
            ).json()

        # The counter must match the rows, or the list view drifts from the
        # thread it summarises.
        assert detail["message_count"] == len(detail["messages"]) == 2


class TestClientDisconnect:
    """What happens when the app is backgrounded mid-reply.

    A different path from a provider failure: the generator is *cancelled*
    from outside rather than raising from within, so the persistence has to
    survive cancellation rather than an exception.
    """

    async def test_a_reply_abandoned_by_the_client_is_still_stored(
        self, session_factory, settings
    ) -> None:
        from sqlalchemy import select

        from nova.ai.offline import OfflineChatProvider
        from nova.models.conversation import Conversation, Message
        from nova.models.user import User
        from nova.services.conversation import ChatStreamer

        async with session_factory() as session:
            user = User(
                email=f"disconnect-{uuid.uuid4().hex[:8]}@example.com",
                password_hash="x",
                display_name="Tester",
            )
            session.add(user)
            await session.flush()
            conversation = Conversation(user_id=user.id)
            session.add(conversation)
            await session.commit()
            conversation_id = conversation.id
            owner_id = user.id

        streamer = ChatStreamer(
            session_factory=session_factory,
            provider=OfflineChatProvider(chunk_size=4),
            settings=settings.ai,
        )
        turn = await streamer.prepare(conversation_id, owner_id, "Cut me off")

        # Read one chunk, then abandon the generator the way a dropped
        # connection does.
        stream = streamer.stream(conversation_id, turn.history)
        first = await anext(stream)
        await stream.aclose()

        assert first

        async with session_factory() as session:
            stored = (
                (
                    await session.execute(
                        select(Message).where(
                            Message.conversation_id == conversation_id,
                            Message.role == "assistant",
                        )
                    )
                )
                .scalars()
                .all()
            )

        # Whatever NOVA managed to say is what it said; discarding it would
        # leave a question with no answer in the thread the user reopens.
        assert len(stored) == 1
        assert stored[0].content == first

    async def test_closing_the_response_body_persists_the_reply_at_once(
        self, session_factory, settings
    ) -> None:
        """The route's body generator, one layer out from the streamer.

        Starlette never closes the body it is handed on a disconnect; the
        garbage collector does, eventually. So closing the body has to be
        enough on its own: it must close the streamer inside it in the same
        step. Delegating with ``async for`` would leave the streamer
        suspended, and the partial reply would be persisted only one
        collection later, as a detached task on a session of its own.
        """
        import inspect

        from sqlalchemy import select

        from nova.api.v1.conversations import _reply_events
        from nova.models.conversation import Conversation, Message
        from nova.models.user import User
        from nova.services.conversation import ChatStreamer

        async with session_factory() as session:
            user = User(
                email=f"disconnect-{uuid.uuid4().hex[:8]}@example.com",
                password_hash="x",
                display_name="Tester",
            )
            session.add(user)
            await session.flush()
            conversation = Conversation(user_id=user.id)
            session.add(conversation)
            await session.commit()
            conversation_id = conversation.id
            owner_id = user.id

        provider = ClosureRecordingProvider()
        streamer = ChatStreamer(
            session_factory=session_factory,
            provider=provider,
            settings=settings.ai,
        )
        turn = await streamer.prepare(conversation_id, owner_id, "Cut me off")

        reply: list[str] = []
        body = _reply_events(streamer, conversation_id, turn, reply)
        await anext(body)  # the echoed user turn
        await anext(body)  # the first delta
        await body.aclose()

        assert reply, "the first delta should have been received"

        # Persisted now -- not after the next collection.
        async with session_factory() as session:
            stored = (
                (
                    await session.execute(
                        select(Message).where(
                            Message.conversation_id == conversation_id,
                            Message.role == "assistant",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(stored) == 1
        # And it is exactly what the client was sent, no more.
        assert stored[0].content == "".join(reply)

        # The streamer was closed rather than left suspended for the
        # collector: no live generator of it is still holding a frame.
        suspended = [
            obj
            for obj in gc.get_objects()
            if inspect.isasyncgen(obj)
            and obj.ag_frame is not None
            and obj.ag_code is ChatStreamer.stream.__code__
        ]
        assert suspended == []

        # And the same close reached the provider's stream inside the
        # streamer, in this task -- not in a finalizer task the collector
        # scheduled after the streamer's own ``async for`` let go of it.
        assert provider.closed_in is asyncio.current_task()
