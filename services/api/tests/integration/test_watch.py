"""The project watcher: state changes become alerts, and nothing else does."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.core.config import IntegrationSettings, ProjectTarget, Settings, WatchSettings
from nova.models.alert import Alert
from nova.repositories.alert import AlertRepository
from nova.services.watch import ProjectWatcher
from nova.tools.projects import build_project_tools
from nova.tools.registry import ToolRegistry
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


class FakeProject:
    """A project whose next answers are scripted, one per probe."""

    def __init__(self) -> None:
        self.answers: list[int | Exception] = []
        self.probes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.probes += 1
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        body = {"dependencies": [{"name": "database", "healthy": answer == 200}]}
        return httpx.Response(answer, json=body)


class RecordingNotifier:
    def __init__(self, *, fails: bool = False) -> None:
        self.received: list[Alert] = []
        self.fails = fails

    async def notify(self, alert: Alert) -> None:
        if self.fails:
            raise RuntimeError("push service down")
        self.received.append(alert)


def _watcher(
    session_factory: async_sessionmaker[AsyncSession],
    project: FakeProject,
    *,
    failures_before_alert: int = 2,
    notifier: Any = None,
) -> ProjectWatcher:
    integrations = IntegrationSettings(
        projects=[ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000")]
    )
    registry = ToolRegistry()
    for tool in build_project_tools(
        integrations, http_client=httpx.AsyncClient(transport=httpx.MockTransport(project.handler))
    ):
        registry.register(tool)
    return ProjectWatcher(
        settings=WatchSettings(enabled=True, failures_before_alert=failures_before_alert),
        integrations=integrations,
        registry=registry,
        session_factory=session_factory,
        notifier=notifier or RecordingNotifier(),
    )


class TestTransitions:
    async def test_one_failure_is_a_blip_and_the_second_is_an_alert(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [503, 503]
        watcher = _watcher(session_factory, project)

        assert await watcher.tick() == []
        (alert,) = await watcher.tick()

        assert alert.kind == "project_down"
        assert alert.message == "SnapWorth answered 503, database failing (2 checks in a row)."

    async def test_staying_down_is_one_alert_not_one_per_tick(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [503, 503, 503, 503]
        watcher = _watcher(session_factory, project)

        raised = [alert for _ in range(4) for alert in await watcher.tick()]

        assert [a.kind for a in raised] == ["project_down"]

    async def test_recovery_is_announced_once(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [503, 503, 200, 200]
        watcher = _watcher(session_factory, project)

        raised = [alert for _ in range(4) for alert in await watcher.tick()]

        assert [a.kind for a in raised] == ["project_down", "project_recovered"]
        assert raised[1].message == "SnapWorth is healthy again."

    async def test_a_blip_between_healthy_checks_never_alerts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [200, 503, 200, 503, 200]
        watcher = _watcher(session_factory, project)

        raised = [alert for _ in range(5) for alert in await watcher.tick()]

        assert raised == []

    async def test_unreachable_counts_as_a_failure_and_says_so(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [httpx.ConnectError("refused"), httpx.ConnectError("refused")]
        watcher = _watcher(session_factory, project)

        await watcher.tick()
        (alert,) = await watcher.tick()

        assert alert.message == "SnapWorth is not responding (2 checks in a row)."

    async def test_a_restart_remembers_what_was_already_down(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A project that fell over last night is not news this morning."""
        project = FakeProject()
        project.answers = [503, 503]
        first = _watcher(session_factory, project)
        await first.tick()
        await first.tick()

        # A fresh process: no in-memory state, only the database.
        project.answers = [503, 200]
        restarted = _watcher(session_factory, project)
        assert await restarted.tick() == []  # still down, already announced
        (alert,) = await restarted.tick()
        assert alert.kind == "project_recovered"

    async def test_alerts_are_persisted_and_the_notifier_is_told(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [503, 503]
        notifier = RecordingNotifier()
        watcher = _watcher(session_factory, project, notifier=notifier)

        await watcher.tick()
        await watcher.tick()

        assert [a.project for a in notifier.received] == ["SnapWorth"]
        async with session_factory() as session:
            stored = await AlertRepository(session).list_recent()
            assert [a.kind for a in stored] == ["project_down"]
            assert stored[0].acknowledged_at is None

    async def test_a_failing_notifier_loses_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [503, 503]
        watcher = _watcher(session_factory, project, notifier=RecordingNotifier(fails=True))

        await watcher.tick()
        (alert,) = await watcher.tick()

        assert alert.kind == "project_down"
        async with session_factory() as session:
            assert await AlertRepository(session).count_unacknowledged() == 1

    async def test_a_threshold_of_one_alerts_on_the_first_failure(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = FakeProject()
        project.answers = [503]
        watcher = _watcher(session_factory, project, failures_before_alert=1)

        (alert,) = await watcher.tick()

        assert alert.message == "SnapWorth answered 503, database failing (1 check in a row)."


async def _raise_two(session_factory: async_sessionmaker[AsyncSession]) -> list[Alert]:
    project = FakeProject()
    project.answers = [503, 503, 200]
    watcher = _watcher(session_factory, project, failures_before_alert=2)
    raised: list[Alert] = []
    for _ in range(3):
        raised += await watcher.tick()
    return raised


class TestAlertRoutes:
    async def test_lists_newest_first_with_the_unacknowledged_count(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _raise_two(session_factory)

        body = (await client.get("/api/v1/alerts", headers=auth_headers)).json()

        assert [a["kind"] for a in body["items"]] == ["project_recovered", "project_down"]
        assert body["unacknowledged"] == 2
        assert body["items"][0]["acknowledged_at"] is None

    async def test_acknowledging_one_clears_only_that_one(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        raised = await _raise_two(session_factory)

        response = await client.post(
            f"/api/v1/alerts/{raised[0].id}/acknowledge", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["acknowledged_at"] is not None

        body = (
            await client.get(
                "/api/v1/alerts", headers=auth_headers, params={"unacknowledged": "true"}
            )
        ).json()
        assert [a["id"] for a in body["items"]] == [str(raised[1].id)]
        assert body["unacknowledged"] == 1

    async def test_acknowledging_everything(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _raise_two(session_factory)

        response = await client.post("/api/v1/alerts/acknowledge-all", headers=auth_headers)

        assert response.json() == {"acknowledged": 2}
        assert (await client.get("/api/v1/alerts", headers=auth_headers)).json()[
            "unacknowledged"
        ] == 0

    async def test_an_unknown_alert_is_a_404(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            f"/api/v1/alerts/{uuid.uuid4()}/acknowledge", headers=auth_headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "alert_not_found"

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/alerts")).status_code == 401


class TestStatusCard:
    async def test_the_dashboard_carries_the_count_and_whether_it_is_watching(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _raise_two(session_factory)

        alerts = (await client.get("/api/v1/system/status", headers=auth_headers)).json()["alerts"]

        assert alerts["unacknowledged"] == 2
        assert alerts["latest"]["kind"] == "project_recovered"
        # The test settings have no projects and the watcher off.
        assert alerts["watching"] is False

    async def test_watching_is_true_only_with_the_watcher_on_and_projects_configured(
        self,
        settings: Settings,
        engine: Any,
        session_factory: Any,
        redis_client: Any,
        auth_headers: dict[str, str],
    ) -> None:
        configured = settings.model_copy(deep=True)
        configured.watch = WatchSettings(enabled=True)
        configured.integrations = IntegrationSettings(
            projects=[ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000")]
        )
        app = build_test_app(
            configured, engine=engine, session_factory=session_factory, redis=redis_client
        )
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://nova.test"
        ) as client:
            alerts = (await client.get("/api/v1/system/status", headers=auth_headers)).json()[
                "alerts"
            ]

        assert alerts["watching"] is True
        assert alerts["interval_seconds"] == 300
