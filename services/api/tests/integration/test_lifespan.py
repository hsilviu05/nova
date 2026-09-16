"""The application factory and the process lifecycle.

Every other test builds an app with its own fixtures parked on ``app.state``,
which is what lets them share a transaction -- and means the real lifespan is
never exercised. That is the code which opens the engine, the Redis client
and the providers, builds the tool registry, and closes all four on the way
out. A leak there shows up as a hung shutdown or an exhausted pool in
production and nowhere else.
"""

from __future__ import annotations

from typing import Any

import pytest

from nova.core.config import Settings, ToolSettings
from nova.main import create_app

pytestmark = pytest.mark.integration


class TestFactory:
    def test_uses_the_settings_it_was_given(self, settings: Settings) -> None:
        """Not the environment.

        `create_app` takes settings as an argument and builds nothing at
        import time; that is what lets the suite construct an app against a
        test database without the module having already connected to
        something.
        """
        app = create_app(settings)
        assert app.state.settings is settings

    def test_the_schema_is_served_outside_production(self, settings: Settings) -> None:
        app = create_app(settings)
        assert app.openapi_url == "/openapi.json"
        assert app.docs_url == "/docs"

    def test_production_serves_no_schema_and_no_docs(self, settings: Settings) -> None:
        """Both are a map of the API's surface, and a deployed NOVA has no
        reason to publish one."""
        production = settings.model_copy(deep=True)
        production.environment = "production"

        app = create_app(production)

        assert app.openapi_url is None
        assert app.docs_url is None

    def test_cors_is_off_unless_an_origin_is_configured(self, settings: Settings) -> None:
        """Correct for a native client, which makes no preflighted
        cross-origin requests."""
        assert settings.security.cors_origins == []
        app = create_app(settings)

        names = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" not in names

    def test_cors_is_added_when_an_origin_is_configured(self, settings: Settings) -> None:
        browser = settings.model_copy(deep=True)
        browser.security.cors_origins = ["https://nova.example.com"]

        app = create_app(browser)

        names = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in names


class TestLifespan:
    """Driven directly rather than through a request.

    ``httpx.ASGITransport`` does not run startup or shutdown, so a test that
    only made a request would exercise none of this and quietly pass.
    """

    async def test_startup_opens_everything_and_shutdown_closes_it(
        self, settings: Settings
    ) -> None:
        configured = settings.model_copy(deep=True)
        configured.tools = ToolSettings(
            system_enabled=True,
            git_enabled=False,
            projects_enabled=False,
            memory_tools_enabled=True,
        )
        app = create_app(configured)

        async with app.router.lifespan_context(app):
            assert app.state.engine is not None
            assert app.state.session_factory is not None
            assert app.state.redis is not None
            assert app.state.token_service is not None
            assert app.state.password_hasher is not None
            assert app.state.chat_provider.name == "offline"
            assert app.state.embedding_provider.name == "lexical"
            # Built from configuration before any request arrives. The memory
            # tools are present because the knowledge dependencies were
            # passed in; the git group is absent because it was switched off.
            assert app.state.tool_registry.has("system_health")
            assert app.state.tool_registry.has("memory_search")
            assert not app.state.tool_registry.has("git_status")

    async def test_everything_opened_is_closed_even_when_one_close_fails(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shutdown path is nested try/finally for a reason.

        A provider holding an HTTP client, a Redis client and an engine are
        all opened; if the first close raises, the rest must still run or the
        process hangs on a pool it never released.
        """
        closed: list[str] = []

        class ExplodingChat(_RecordingProvider):
            async def aclose(self) -> None:
                closed.append("chat")
                raise RuntimeError("the client was already gone")

        class RecordingEmbedder:
            name = "recording"
            dimensions = 1536

            async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
                raise NotImplementedError

            async def aclose(self) -> None:
                closed.append("embeddings")

        monkeypatch.setattr("nova.main.build_chat_provider", lambda _: ExplodingChat(closed))
        monkeypatch.setattr("nova.main.build_embedding_provider", lambda _: RecordingEmbedder())

        app = create_app(settings)
        redis_closed: list[str] = []

        with pytest.raises(RuntimeError):
            async with app.router.lifespan_context(app):
                original_close = app.state.redis.aclose

                async def record_and_close() -> None:
                    redis_closed.append("redis")
                    await original_close()

                app.state.redis.aclose = record_and_close  # type: ignore[method-assign]

        assert closed == ["chat", "embeddings"]
        assert redis_closed == ["redis"]

    async def test_a_registry_built_without_knowledge_has_no_memory_tools(
        self, settings: Settings
    ) -> None:
        """The memory tools need a session factory and an embedder. Without
        them the group is skipped rather than registered half-wired."""
        from nova.core.config import IntegrationSettings
        from nova.tools.registry import build_registry

        registry = build_registry(
            tools=ToolSettings(memory_tools_enabled=True),
            integrations=IntegrationSettings(),
            knowledge=None,
        )

        assert not registry.has("memory_search")


class _RecordingProvider:
    """A chat provider that records when it was closed."""

    name = "recording"
    model = "recording-model"
    supports_tools = False

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def aclose(self) -> None:
        self._log.append("chat")

    async def stream(self, request: Any) -> Any:  # pragma: no cover - never driven
        raise NotImplementedError

    async def complete(self, request: Any) -> Any:  # pragma: no cover - never driven
        raise NotImplementedError
