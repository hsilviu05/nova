"""The dashboard.

What replaced device telemetry. The shape of the response is a contract with
the iOS home screen, so these are as much contract tests as behaviour tests.

The recurring theme: a card that cannot be filled is a *finding*, never a
failure of the screen. A dashboard that returns 500 because the model server
is down is a dashboard that stops working exactly when it is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from nova.ai.base import ChatCompletion, ChatRequest, StreamCompleted, StreamEvent
from nova.ai.errors import AIUnavailableError
from nova.core.config import Settings
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


class UnreachableProvider:
    """A local model server that is not running.

    The single most likely state of a NOVA on a laptop that just woke up, so
    it deserves a test rather than an assumption.
    """

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return "qwen3.8:latest"

    @property
    def supports_tools(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent, None]:
        raise AIUnavailableError(
            "The local model server is not reachable. Is Ollama running?",
            code="ai_local_model_unreachable",
        )
        yield StreamCompleted()  # pragma: no cover - keeps this a generator

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        raise AIUnavailableError(
            "The local model server is not reachable. Is Ollama running?",
            code="ai_local_model_unreachable",
        )


def _client(
    settings: Settings,
    engine: Any,
    session_factory: Any,
    redis_client: Any,
    provider: Any = None,
) -> AsyncClient:
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
                "email": f"dash-{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Tester",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


class TestStatus:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/system/status")).status_code == 401

    async def test_returns_every_card_the_home_screen_needs(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """One response rather than six endpoints: the screen needs all of it
        before it can render anything."""
        body = (await client.get("/api/v1/system/status", headers=auth_headers)).json()

        assert body.keys() >= {
            "generated_at",
            "ai",
            "host",
            "dependencies",
            "projects",
            "memory",
            "tools",
            "recent_activity",
        }

    async def test_reports_the_model_that_would_answer(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        body = (await client.get("/api/v1/system/status", headers=auth_headers)).json()

        assert body["ai"]["provider"] == "offline"
        assert body["ai"]["online"] is True
        # Honest about what it is: the app shows this so nobody mistakes
        # canned replies for a working model.
        assert "No model is configured" in body["ai"]["detail"]

    async def test_reports_the_machine(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        host = (await client.get("/api/v1/system/status", headers=auth_headers)).json()["host"]

        assert host["hostname"]
        assert host["cpu_count"] >= 1
        assert 0 <= host["disk_percent_used"] <= 100

    async def test_reports_dependency_health(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        body = (await client.get("/api/v1/system/status", headers=auth_headers)).json()

        named = {d["name"]: d["healthy"] for d in body["dependencies"]}
        assert named == {"postgres": True, "redis": True}

    async def test_reports_what_nova_can_do(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tools = (await client.get("/api/v1/system/status", headers=auth_headers)).json()["tools"]

        assert tools["shell_enabled"] is False
        assert tools["count"] >= 0
        assert isinstance(tools["groups"], list)

    async def test_a_model_server_that_is_down_is_a_finding_not_a_failure(
        self, settings, engine, session_factory, redis_client
    ) -> None:
        """The most important assertion on this screen.

        A terminal whose whole job is telling you what is running has to keep
        working when something is not.
        """
        async with _client(
            settings, engine, session_factory, redis_client, UnreachableProvider()
        ) as client:
            headers = await _signed_in(client)
            response = await client.get("/api/v1/system/status", headers=headers)

        assert response.status_code == 200
        ai = response.json()["ai"]
        assert ai["online"] is False
        assert "Ollama" in ai["detail"]

    async def test_no_projects_configured_is_an_empty_list(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Not an error. A NOVA with nothing to monitor is correctly
        configured."""
        body = (await client.get("/api/v1/system/status", headers=auth_headers)).json()
        assert body["projects"] == []


class TestMemoryCard:
    async def test_counts_and_names_what_nova_has_learned(
        self, client: AsyncClient, auth_headers: dict[str, str], session_factory: Any
    ) -> None:
        from nova.ai.registry import build_embedding_provider
        from nova.core.config import AISettings
        from nova.models.memory import Memory

        me = (await client.get("/api/v1/users/me", headers=auth_headers)).json()
        embeddings = build_embedding_provider(AISettings())
        vectors = await embeddings.embed(["Prefers PostgreSQL for new projects"])

        async with session_factory() as session:
            session.add(
                Memory(
                    user_id=uuid.UUID(me["id"]),
                    content="Prefers PostgreSQL for new projects",
                    category="preference",
                    importance=0.8,
                    confidence=1.0,
                    embedding=vectors[0],
                    embedding_provider=embeddings.name,
                )
            )
            await session.commit()

        memory = (await client.get("/api/v1/system/status", headers=auth_headers)).json()["memory"]

        assert memory["total"] == 1
        assert memory["recent"] == ["Prefers PostgreSQL for new projects"]

    async def test_the_memory_card_is_scoped_to_the_caller(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """An account on a shared NOVA must not see another's memories, not
        even as a count."""
        other = await _signed_in(client)

        body = (await client.get("/api/v1/system/status", headers=other)).json()
        assert body["memory"]["total"] == 0
