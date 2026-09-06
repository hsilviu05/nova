"""Conversations end to end: threads, replies, streaming, isolation."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def _conversation(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post("/api/v1/conversations", headers=headers, json={})
    assert response.status_code == 201, response.text
    return response.json()


async def _second_user(client: AsyncClient) -> dict[str, str]:
    body = (
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"other-{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Other",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Split a Server-Sent Event stream into (event, data) pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name = ""
        payload = "{}"
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                payload = line.removeprefix("data: ")
        events.append((name, json.loads(payload)))
    return events


class TestConversationLifecycle:
    async def test_starts_empty_and_untitled(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)

        assert conversation["message_count"] == 0
        # The title comes from the first message rather than being asked for.
        assert conversation["title"] is None
        assert conversation["last_message_at"] is None

    async def test_lists_only_your_own(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        mine = await _conversation(client, auth_headers)
        other_headers = await _second_user(client)
        await _conversation(client, other_headers)

        listing = (await client.get("/api/v1/conversations", headers=auth_headers)).json()
        assert [c["id"] for c in listing] == [mine["id"]]

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/conversations")).status_code == 401

    async def test_deletes(self, client: AsyncClient, auth_headers: dict[str, str]) -> None:
        conversation = await _conversation(client, auth_headers)

        assert (
            await client.delete(f"/api/v1/conversations/{conversation['id']}", headers=auth_headers)
        ).status_code == 204

        assert (await client.get("/api/v1/conversations", headers=auth_headers)).json() == []

    async def test_another_users_conversation_is_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """404 rather than 403: confirming the id exists would itself leak."""
        conversation = await _conversation(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.get(
            f"/api/v1/conversations/{conversation['id']}", headers=other_headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"


class TestSendingMessages:
    async def test_returns_both_turns(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)

        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=auth_headers,
            json={"content": "Are you there?"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["user_message"]["content"] == "Are you there?"
        assert body["user_message"]["role"] == "user"
        assert body["assistant_message"]["role"] == "assistant"
        assert body["assistant_message"]["content"]

    async def test_records_which_model_replied(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # So a reply can be traced to a provider and a bill later.
        conversation = await _conversation(client, auth_headers)
        body = (
            await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                headers=auth_headers,
                json={"content": "Hello"},
            )
        ).json()

        assert body["assistant_message"]["model"] == "offline-deterministic"
        assert body["assistant_message"]["latency_ms"] is not None

    async def test_titles_the_conversation_from_the_first_message(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=auth_headers,
            json={"content": "What did I do yesterday?"},
        )

        detail = (
            await client.get(f"/api/v1/conversations/{conversation['id']}", headers=auth_headers)
        ).json()
        assert detail["title"] == "What did I do yesterday?"

    async def test_a_long_first_message_is_truncated_on_a_word_boundary(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=auth_headers,
            json={"content": " ".join(["antidisestablishmentarianism"] * 6)},
        )

        title = (
            await client.get(f"/api/v1/conversations/{conversation['id']}", headers=auth_headers)
        ).json()["title"]

        assert title.endswith("…")
        assert len(title) <= 61
        # Cut at a space, so the visible part is whole words.
        assert not title.removesuffix("…").endswith("anism"[:3])

    async def test_history_accumulates_in_order(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        for text in ("first", "second", "third"):
            await client.post(
                f"/api/v1/conversations/{conversation['id']}/messages",
                headers=auth_headers,
                json={"content": text},
            )

        detail = (
            await client.get(f"/api/v1/conversations/{conversation['id']}", headers=auth_headers)
        ).json()

        assert detail["message_count"] == 6
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["user", "assistant"] * 3
        assert [m["content"] for m in detail["messages"] if m["role"] == "user"] == [
            "first",
            "second",
            "third",
        ]

    @pytest.mark.parametrize("content", ["", "   ", "\n\t "])
    async def test_rejects_a_blank_message(
        self, client: AsyncClient, auth_headers: dict[str, str], content: str
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=auth_headers,
            json={"content": content},
        )
        assert response.status_code == 422

    async def test_rejects_an_oversized_message(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=auth_headers,
            json={"content": "x" * 5000},
        )
        assert response.status_code == 422

    async def test_cannot_post_into_another_users_conversation(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=other_headers,
            json={"content": "Let me in"},
        )
        assert response.status_code == 404

    async def test_unknown_conversation_is_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            f"/api/v1/conversations/{uuid.uuid4()}/messages",
            headers=auth_headers,
            json={"content": "Hello"},
        )
        assert response.status_code == 404


class TestStreaming:
    async def test_streams_the_reply_as_events(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)

        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=auth_headers,
            json={"content": "Tell me something"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _parse_sse(response.text)
        names = [name for name, _ in events]

        # The stored user turn first, so the client can replace its
        # optimistic copy with the real id.
        assert names[0] == "message"
        assert names[-1] == "done"
        assert "delta" in names

    async def test_deltas_reassemble_into_the_stored_reply(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)

        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=auth_headers,
            json={"content": "Tell me something"},
        )
        streamed = "".join(
            payload["text"] for name, payload in _parse_sse(response.text) if name == "delta"
        )

        detail = (
            await client.get(f"/api/v1/conversations/{conversation['id']}", headers=auth_headers)
        ).json()
        assistant = [m for m in detail["messages"] if m["role"] == "assistant"]

        # What the client rendered must equal what was persisted, or the
        # thread will differ from the live view on next open.
        assert len(assistant) == 1
        assert assistant[0]["content"] == streamed

    async def test_persists_both_turns(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The streamer writes through its own sessions.

        A streaming body runs after the endpoint returns, when the
        request-scoped session is already closed -- so this failing would
        mean the reply was never stored.
        """
        conversation = await _conversation(client, auth_headers)
        await client.post(
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=auth_headers,
            json={"content": "Hello"},
        )

        detail = (
            await client.get(f"/api/v1/conversations/{conversation['id']}", headers=auth_headers)
        ).json()
        assert detail["message_count"] == 2
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]

    async def test_unknown_conversation_fails_before_the_stream_starts(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """A normal 404, not an error event inside a rendering stream."""
        response = await client.post(
            f"/api/v1/conversations/{uuid.uuid4()}/stream",
            headers=auth_headers,
            json={"content": "Hello"},
        )

        assert response.status_code == 404
        assert not response.headers["content-type"].startswith("text/event-stream")

    async def test_another_users_conversation_fails_before_the_stream(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=other_headers,
            json={"content": "Let me in"},
        )
        assert response.status_code == 404

    async def test_rejects_a_blank_message_before_streaming(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        conversation = await _conversation(client, auth_headers)
        response = await client.post(
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=auth_headers,
            json={"content": "  "},
        )
        assert response.status_code == 422
