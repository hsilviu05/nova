"""Watching the configured projects, and saying so when one changes state.

"Tell me when the deploy fails" is a scheduler and a notifier. The scheduler
is here: one task per process that probes every configured project each
interval through the same ``project_health`` tool the dashboard and chat
use, and turns a *change* of state into an :class:`Alert` row. Not every
failure -- a project that has been down for an hour is one alert, not
twelve -- and not the first failure either, because one dropped probe on
home Wi-Fi is a blip and alerting on it would train everyone to ignore
alerts.

The notifier is a seam. The default writes to the log; the phone finds the
alerts by polling and raises its own notification while the app is open.
Anything that pushes to a phone with the app closed needs APNs, which
needs an Apple key and an entitlement, and is a class of its own behind
the same protocol.

State survives a restart: on the first tick the watcher reads the latest
alert per project and treats "down" as still down, so a project that fell
over last night is not announced again this morning as if it were news.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.core.clock import utc_now
from nova.core.config import IntegrationSettings, WatchSettings
from nova.core.logging import get_logger
from nova.models.alert import Alert
from nova.repositories.alert import AlertRepository
from nova.schemas.system import ProjectStatus
from nova.services.system import check_projects
from nova.tools.base import ToolContext
from nova.tools.registry import ToolRegistry

logger = get_logger(__name__)


class Notifier(Protocol):
    """Somewhere an alert goes besides the database."""

    async def notify(self, alert: Alert) -> None: ...


class LoggingNotifier:
    """The default: the alert is a log line, and the app polls for it."""

    async def notify(self, alert: Alert) -> None:
        logger.warning("alert", kind=alert.kind, project=alert.project, message=alert.message)


@dataclass
class _ProjectState:
    down: bool = False
    consecutive_failures: int = 0


@dataclass
class ProjectWatcher:
    settings: WatchSettings
    integrations: IntegrationSettings
    registry: ToolRegistry
    session_factory: async_sessionmaker[AsyncSession]
    notifier: Notifier = field(default_factory=LoggingNotifier)

    _state: dict[str, _ProjectState] = field(default_factory=dict, init=False)
    _primed: bool = field(default=False, init=False)

    async def run(self) -> None:
        """Probe forever, one interval apart. Meant to be a task."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed tick must not end the watch
                logger.warning("watch_tick_failed", error=type(exc).__name__)
            await asyncio.sleep(self.settings.interval_seconds)

    async def tick(self) -> list[Alert]:
        """Probe every project once and return the alerts that raised."""
        # No user is asking; the tool does not use the field for a health
        # check, and the invocation is not audited, same as the dashboard's.
        statuses = await check_projects(
            self.registry, self.integrations, context=ToolContext(user_id=None)
        )
        if not statuses:
            return []

        raised: list[Alert] = []
        async with self.session_factory() as session:
            repository = AlertRepository(session)
            if not self._primed:
                await self._prime(repository)

            for status in statuses:
                alert = self._transition(status)
                if alert is not None:
                    repository.add(alert)
                    raised.append(alert)
            await session.commit()

        for alert in raised:
            try:
                await self.notifier.notify(alert)
            except Exception as exc:  # the row is written; delivery is best effort
                logger.warning(
                    "alert_notify_failed", project=alert.project, error=type(exc).__name__
                )
        return raised

    async def _prime(self, repository: AlertRepository) -> None:
        latest = await repository.latest_kind_by_project()
        for project, kind in latest.items():
            self._state[project] = _ProjectState(down=kind == "project_down")
        self._primed = True

    def _transition(self, status: ProjectStatus) -> Alert | None:
        state = self._state.setdefault(status.name, _ProjectState())
        if status.healthy:
            state.consecutive_failures = 0
            if not state.down:
                return None
            state.down = False
            return Alert(
                kind="project_recovered",
                project=status.name,
                message=f"{status.name} is healthy again.",
                created_at=utc_now(),
            )

        state.consecutive_failures += 1
        if state.down or state.consecutive_failures < self.settings.failures_before_alert:
            return None
        state.down = True
        return Alert(
            kind="project_down",
            project=status.name,
            message=_down_message(status, state.consecutive_failures),
            created_at=utc_now(),
        )


def _down_message(status: ProjectStatus, failures: int) -> str:
    checks = "check" if failures == 1 else "checks"
    if not status.reachable:
        return f"{status.name} is not responding ({failures} {checks} in a row)."
    detail = f"answered {status.status_code}" if status.status_code else "is unhealthy"
    failing = sorted(name for name, ok in status.dependencies.items() if not ok)
    if failing:
        detail += ", " + ", ".join(failing) + " failing"
    return f"{status.name} {detail} ({failures} {checks} in a row)."
