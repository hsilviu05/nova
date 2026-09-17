"""The dashboard's two slow sections, driven directly.

``overview`` gathers six things concurrently. The route tests cover the shape
of what comes back; these cover what each section does when the thing it is
asking about is slow, down, or answering nonsense -- which is the state the
dashboard exists to report and the one hardest to arrange over HTTP.

The rule they enforce is the one in the route tests' docstring: a card that
cannot be filled is a finding, never a failure. A 500 here means the home
screen goes blank exactly when something is wrong.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nova.ai.base import ChatCompletion, ChatRequest, StreamCompleted, StreamEvent
from nova.ai.errors import AIProviderError
from nova.ai.registry import build_embedding_provider
from nova.core.config import IntegrationSettings, ProjectTarget, Settings, ToolSettings
from nova.repositories.alert import AlertRepository
from nova.repositories.memory import MemoryRepository
from nova.repositories.tool_invocation import ToolInvocationRepository
from nova.services.health import HealthService
from nova.services.system import SystemStatusService
from nova.tools.projects import build_project_tools
from nova.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


class _Provider:
    """A chat provider whose ``complete`` does whatever the test needs."""

    name = "ollama"
    model = "qwen3.8:latest"
    supports_tools = True

    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour

    async def aclose(self) -> None:
        return None

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent, None]:
        yield StreamCompleted()  # pragma: no cover - the probe never streams

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        return await self._behaviour(request)


def service(
    settings: Settings,
    session: AsyncSession,
    *,
    provider: Any,
    registry: ToolRegistry | None = None,
) -> SystemStatusService:
    """Built directly.

    ``_ai_status`` and ``_project_status`` touch neither the database nor
    Redis, so the health service is never driven here -- the route tests
    cover the dependency card.
    """
    return SystemStatusService(
        settings=settings,
        provider=provider,
        health=HealthService(engine=None, redis=None),  # type: ignore[arg-type]
        registry=registry or ToolRegistry(),
        memories=MemoryRepository(session),
        invocations=ToolInvocationRepository(session),
        embeddings=build_embedding_provider(settings.ai),
        alerts=AlertRepository(session),
    )


class TestTheAICard:
    def _service_for(
        self, settings: Settings, session: AsyncSession, provider: Any
    ) -> SystemStatusService:
        return service(settings, session, provider=provider)

    async def test_a_model_that_answers_is_reported_with_its_latency(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """The probe is a real completion, not a ping.

        A model server that accepts connections and then never produces a
        token is the common failure, and only asking it for a word finds it.
        """

        async def answers(request: ChatRequest) -> ChatCompletion:
            assert request.max_tokens == 8
            return ChatCompletion(text="ok", model="qwen3.8:latest")

        status = await service(settings, session, provider=_Provider(answers))._ai_status()

        assert status.online is True
        assert status.latency_ms is not None
        assert status.latency_ms >= 0
        assert status.detail is None

    async def test_polling_the_dashboard_does_not_re_probe_the_model_each_time(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """The reason the probe is cached at all.

        The phone refreshes every fifteen seconds and the probe is a real
        completion, so without a cache a device sitting in a desk stand asks
        a local model to generate something four times a minute for as long
        as the screen is on -- pinning several gigabytes of weights and
        keeping the GPU busy indefinitely.
        """
        import nova.services.system as module

        calls = 0

        async def counts(request: ChatRequest) -> ChatCompletion:
            nonlocal calls
            calls += 1
            return ChatCompletion(text="ok", model="qwen3.8:latest")

        module._probe_cache = None
        service = self._service_for(settings, session, _Provider(counts))
        try:
            first = await service._ai_status()
            for _ in range(9):
                await service._ai_status()
        finally:
            module._probe_cache = None

        assert calls == 1
        assert first.online is True

    async def test_a_model_that_is_down_is_not_re_probed_every_poll_either(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """A failing Ollama is exactly when you least want a completion
        request every fifteen seconds."""
        import nova.services.system as module

        calls = 0

        async def refuses(request: ChatRequest) -> ChatCompletion:
            nonlocal calls
            calls += 1
            raise AIProviderError("Ollama is not running.", code="ai_local_model_unreachable")

        module._probe_cache = None
        service = self._service_for(settings, session, _Provider(refuses))
        try:
            first = await service._ai_status()
            await service._ai_status()
        finally:
            module._probe_cache = None

        assert calls == 1
        assert first.online is False

    async def test_a_stale_reading_is_discarded(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """Bounded staleness, not a permanent answer: a model that comes back
        has to be noticed."""
        import nova.services.system as module

        calls = 0

        async def counts(request: ChatRequest) -> ChatCompletion:
            nonlocal calls
            calls += 1
            return ChatCompletion(text="ok", model="qwen3.8:latest")

        module._probe_cache = None
        service = self._service_for(settings, session, _Provider(counts))
        try:
            await service._ai_status()
            # Age the cached entry past its window rather than sleeping
            # through it.
            probed_at, cached = module._probe_cache
            module._probe_cache = (probed_at - module._PROBE_CACHE_SECONDS - 1, cached)
            await service._ai_status()
        finally:
            module._probe_cache = None

        assert calls == 2

    async def test_the_offline_provider_is_never_probed_or_cached(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """It answers from a table. Asking it anything is pure cost."""
        import nova.services.system as module

        class Offline(_Provider):
            name = "offline"

        async def unused(request: ChatRequest) -> ChatCompletion:  # pragma: no cover
            raise AssertionError("the offline provider is not probed")

        module._probe_cache = None
        await self._service_for(settings, session, Offline(unused))._ai_status()

        assert module._probe_cache is None

    async def test_a_model_that_does_not_answer_in_time_is_reported_as_offline(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """A 27B model on a laptop can take minutes for a first token.

        The dashboard refreshes every fifteen seconds, so the probe has a
        budget and an honest answer when it runs out -- rather than holding
        the whole response open behind the slowest thing on the machine.
        """

        async def never(request: ChatRequest) -> ChatCompletion:
            await asyncio.sleep(60)
            raise AssertionError("should have timed out")  # pragma: no cover

        import nova.services.system as module

        original = module._PROBE_TIMEOUT_SECONDS
        module._PROBE_TIMEOUT_SECONDS = 0.05
        try:
            status = await service(settings, session, provider=_Provider(never))._ai_status()
        finally:
            module._PROBE_TIMEOUT_SECONDS = original

        assert status.online is False
        assert status.detail is not None
        assert "did not answer within" in status.detail

    async def test_a_provider_error_is_reported_in_its_own_words(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """ "Ollama does not have qwen3.8. Run: ollama pull qwen3.8" is a
        message somebody can act on; "AI unavailable" is not."""

        async def refuses(request: ChatRequest) -> ChatCompletion:
            raise AIProviderError(
                "Ollama does not have qwen3.8:latest. Run: ollama pull qwen3.8",
                code="ai_model_not_pulled",
            )

        status = await service(settings, session, provider=_Provider(refuses))._ai_status()

        assert status.online is False
        assert status.detail == "Ollama does not have qwen3.8:latest. Run: ollama pull qwen3.8"

    async def test_the_offline_provider_is_online_because_it_will_answer(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """It answers every message. What it says is another matter, and the
        provider name is what tells the app to show that."""

        class Offline(_Provider):
            name = "offline"

        async def unused(request: ChatRequest) -> ChatCompletion:  # pragma: no cover
            raise AssertionError("the offline provider is not probed")

        status = await service(settings, session, provider=Offline(unused))._ai_status()

        assert status.online is True
        assert status.detail is not None
        assert "canned" in status.detail


class TestTheProjectsCard:
    """Checked through the ``project_health`` tool rather than a second HTTP
    call, so "check SnapWorth" in chat and the dashboard card can never
    disagree about whether something is up.
    """

    def _configured(
        self, settings: Settings, handler: Any, *, projects: list[ProjectTarget]
    ) -> tuple[Settings, ToolRegistry]:
        configured = settings.model_copy(deep=True)
        configured.integrations = IntegrationSettings(projects=projects)
        configured.tools = ToolSettings(projects_enabled=True)

        registry = ToolRegistry()
        for tool in build_project_tools(
            configured.integrations,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ):
            registry.register(tool)
        return configured, registry

    async def test_every_configured_project_is_checked(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            healthy = "50" in str(request.url)
            return httpx.Response(
                200 if healthy else 503,
                json={"dependencies": [{"name": "database", "healthy": healthy}]},
            )

        configured, registry = self._configured(
            settings,
            handler,
            projects=[
                ProjectTarget(
                    name="SnapWorth",
                    base_url="http://192.168.1.50:8000",
                    description="Receipt valuation",
                ),
                ProjectTarget(name="AIInterviewCoach", base_url="http://192.168.1.51:8000"),
            ],
        )

        statuses = await service(
            configured, session, provider=_Provider(None), registry=registry
        )._project_status(uuid.uuid4())

        by_name = {status.name: status for status in statuses}
        assert by_name["SnapWorth"].healthy is True
        assert by_name["SnapWorth"].status_code == 200
        assert by_name["SnapWorth"].dependencies == {"database": True}
        # Carried from configuration rather than from the project's response,
        # so a project that is down still has a label on the card.
        assert by_name["SnapWorth"].description == "Receipt valuation"
        assert by_name["AIInterviewCoach"].healthy is False
        assert by_name["AIInterviewCoach"].description is None

    async def test_a_project_that_cannot_be_reached_is_reported_as_such(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        configured, registry = self._configured(
            settings,
            handler,
            projects=[ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000")],
        )

        statuses = await service(
            configured, session, provider=_Provider(None), registry=registry
        )._project_status(uuid.uuid4())

        assert statuses[0].reachable is False
        assert statuses[0].healthy is False

    async def test_a_check_that_raises_costs_one_card_not_the_dashboard(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """Including a ToolError.

        A project NOVA cannot check is a project reported as unreachable. The
        alternative is a 500 for the whole home screen because one entry in
        configuration has a typo in it.
        """

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("the tool should have failed before this")

        configured, registry = self._configured(
            settings,
            handler,
            projects=[ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000")],
        )

        async def explode(payload: Any, context: Any) -> Any:
            raise RuntimeError("the tool is broken")

        registry.get("project_health").execute = explode  # type: ignore[method-assign]

        statuses = await service(
            configured, session, provider=_Provider(None), registry=registry
        )._project_status(uuid.uuid4())

        assert len(statuses) == 1
        assert statuses[0].name == "SnapWorth"
        assert statuses[0].reachable is False

    async def test_a_check_that_hangs_is_given_up_on(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """A project whose health endpoint accepts the connection and never
        answers would otherwise hold the dashboard open indefinitely."""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("never reached")

        configured, registry = self._configured(
            settings,
            handler,
            projects=[ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000")],
        )
        configured.integrations.request_timeout_seconds = 0.01

        async def hang(payload: Any, context: Any) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("should have timed out")  # pragma: no cover

        registry.get("project_health").execute = hang  # type: ignore[method-assign]

        statuses = await service(
            configured, session, provider=_Provider(None), registry=registry
        )._project_status(uuid.uuid4())

        assert statuses[0].reachable is False

    async def test_nothing_configured_is_an_empty_list_not_a_check(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        statuses = await service(settings, session, provider=_Provider(None))._project_status(
            uuid.uuid4()
        )

        assert statuses == []

    async def test_projects_configured_without_the_tool_registered_are_skipped(
        self, settings: Settings, session: AsyncSession
    ) -> None:
        """Configuration and capability are separate switches.

        Projects listed with the group disabled means somebody turned the
        tools off; the card is empty rather than the request failing on a
        missing tool.
        """
        configured = settings.model_copy(deep=True)
        configured.integrations = IntegrationSettings(
            projects=[ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000")]
        )

        statuses = await service(
            configured, session, provider=_Provider(None), registry=ToolRegistry()
        )._project_status(uuid.uuid4())

        assert statuses == []
