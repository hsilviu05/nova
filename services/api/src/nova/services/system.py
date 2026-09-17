"""Assembling the dashboard.

This is what replaced device telemetry. The question is the same shape --
"what is the state of the thing NOVA watches" -- but the thing is now a
machine, a set of services, and NOVA's own memory rather than a robot's
battery.

Every section is gathered concurrently and every one degrades on its own. A
GitHub outage, a project that is down, and a model server that is not running
are all *findings* for this screen rather than failures of it: a dashboard
that returns 500 because one card could not be filled is a dashboard that
stops working exactly when it is needed.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import uuid
from datetime import timedelta

from nova.ai.base import ChatMessage, ChatProvider, ChatRequest, EmbeddingProvider
from nova.ai.errors import AIProviderError
from nova.core.clock import utc_now
from nova.core.config import IntegrationSettings, Settings
from nova.core.logging import get_logger
from nova.repositories.memory import MemoryRepository
from nova.repositories.tool_invocation import ToolInvocationRepository
from nova.schemas.system import (
    ActivityEntry,
    AIStatus,
    HostStatus,
    MemoryStatus,
    ProjectStatus,
    SystemStatus,
    ToolsStatus,
)
from nova.services.health import HealthService
from nova.tools.base import ToolContext
from nova.tools.registry import ToolRegistry
from nova.tools.system import load_average, memory_usage

logger = get_logger(__name__)

# Memories named on the dashboard. Enough to show NOVA is learning, few
# enough that the card stays a glance rather than a list.
RECENT_MEMORIES = 3
RECENT_ACTIVITY = 8

# The reachability probe asks the model for one token. Cheap, and it proves
# the thing that matters -- that a model is loaded and answering -- which
# pinging the HTTP port does not.
_PROBE_TIMEOUT_SECONDS = 6.0
_PROBE_PROMPT = "Reply with the single word: ok"


class SystemStatusService:
    """Builds the one response the home screen renders."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: ChatProvider,
        health: HealthService,
        registry: ToolRegistry,
        memories: MemoryRepository,
        invocations: ToolInvocationRepository,
        embeddings: EmbeddingProvider,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._health = health
        self._registry = registry
        self._memories = memories
        self._invocations = invocations
        self._embeddings = embeddings

    async def overview(self, owner_id: uuid.UUID) -> SystemStatus:
        """Everything at once, gathered concurrently.

        The database reads are sequential among themselves -- they share one
        session, and a session is not safe to use from two tasks -- but they
        run alongside the probes, which are the slow part.
        """
        ai, readiness, host, projects = await asyncio.gather(
            self._ai_status(),
            self._health.check_readiness(),
            self._host_status(),
            self._project_status(owner_id),
        )

        since = utc_now() - timedelta(days=1)
        memory = await self._memory_status(owner_id)
        recent = await self._invocations.list_for_owner(owner_id, limit=RECENT_ACTIVITY)
        invocations_today = await self._invocations.count_since(owner_id, since=since)
        failures_today = await self._invocations.count_failures_since(owner_id, since=since)

        return SystemStatus(
            generated_at=utc_now(),
            ai=ai,
            host=host,
            # Reused from the readiness probe rather than re-derived, so
            # /ready and the dashboard can never disagree about whether
            # Postgres is up.
            dependencies=readiness.dependencies,
            projects=projects,
            memory=memory,
            tools=ToolsStatus(
                count=len(self._registry),
                groups=sorted(group.value for group in self._registry.groups()),
                shell_enabled=self._settings.tools.shell_enabled,
                invocations_today=invocations_today,
                failures_today=failures_today,
            ),
            recent_activity=[ActivityEntry.model_validate(row) for row in recent],
        )

    # -- sections ---------------------------------------------------------

    async def _ai_status(self) -> AIStatus:
        """Whether a model is actually going to answer.

        The offline provider is reported as online, because it is: it will
        answer every message. What it will say is another matter, and the
        provider name is what tells the app to show that.
        """
        base = AIStatus(
            provider=self._provider.name,
            model=self._provider.model,
            online=True,
            supports_tools=self._provider.supports_tools,
        )
        if self._provider.name == "offline":
            return base.model_copy(update={"detail": "No model is configured; replies are canned."})

        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(
                self._provider.complete(
                    ChatRequest(
                        system="You are a health probe.",
                        messages=[ChatMessage(role="user", content=_PROBE_PROMPT)],
                        max_tokens=8,
                    )
                ),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return base.model_copy(
                update={
                    "online": False,
                    "detail": f"The model did not answer within {_PROBE_TIMEOUT_SECONDS:g}s.",
                }
            )
        except AIProviderError as exc:
            return base.model_copy(update={"online": False, "detail": exc.message})
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("ai_probe_failed", error=type(exc).__name__)
            return base.model_copy(
                update={"online": False, "detail": "The model provider failed unexpectedly."}
            )

        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        return base.model_copy(update={"latency_ms": round(elapsed, 1)})

    async def _host_status(self) -> HostStatus:
        """This machine, read through the system tools' own helpers.

        Reusing them rather than reading ``/proc`` a second time means the
        dashboard and ``system_health`` can never disagree about the numbers,
        which would be a very confusing bug to chase.
        """
        cpus = os.cpu_count() or 1
        load = load_average()
        memory = await memory_usage()
        disk = await asyncio.to_thread(shutil.disk_usage, os.path.expanduser("~"))

        return HostStatus(
            hostname=platform.node(),
            platform=f"{platform.system()} {platform.release()}",
            cpu_count=cpus,
            load_per_core=round(load[0] / cpus, 2) if load else None,
            memory_percent_used=memory["percent_used"] if memory else None,
            disk_percent_used=round(disk.used / disk.total * 100, 1),
        )

    async def _project_status(self, owner_id: uuid.UUID) -> list[ProjectStatus]:
        """Health for every configured project, checked in parallel.

        Goes through the tool rather than duplicating the HTTP call, so the
        dashboard and "check SnapWorth" in chat answer from exactly the same
        code. The invocations are not audited: this is a screen refreshing,
        not somebody asking NOVA to do something, and filling the audit log
        with dashboard polls would bury the entries that matter.
        """
        integrations: IntegrationSettings = self._settings.integrations
        if not integrations.projects or not self._registry.has("project_health"):
            return []

        tool = self._registry.get("project_health")
        context = ToolContext(user_id=owner_id)
        descriptions = {project.name: project.description for project in integrations.projects}

        async def check(name: str) -> ProjectStatus:
            try:
                payload = tool.spec.input_model.model_validate({"project": name})
                result = await asyncio.wait_for(
                    tool.execute(payload, context),
                    timeout=integrations.request_timeout_seconds + 2,
                )
            except Exception as exc:
                # Including ToolError: a project NOVA cannot check is a
                # project reported as unreachable, not a broken dashboard.
                logger.info("project_check_failed", project=name, error=type(exc).__name__)
                return ProjectStatus(
                    name=name,
                    healthy=False,
                    reachable=False,
                    description=descriptions.get(name),
                )

            data = result.data
            return ProjectStatus(
                name=name,
                healthy=bool(data.get("healthy")),
                reachable=bool(data.get("reachable")),
                description=descriptions.get(name),
                status_code=data.get("status_code"),
                latency_ms=data.get("latency_ms"),
                dependencies=data.get("dependencies") or {},
            )

        return list(
            await asyncio.gather(*(check(project.name) for project in integrations.projects))
        )

    async def _memory_status(self, owner_id: uuid.UUID) -> MemoryStatus:
        total = await self._memories.count_for_owner(owner_id)
        recent = await self._memories.list_recent(owner_id, limit=RECENT_MEMORIES)
        stale = await self._memories.count_stale(self._embeddings.name, owner_id=owner_id)
        return MemoryStatus(
            total=total,
            recent=[memory.content for memory in recent],
            embedding_provider=self._embeddings.name,
            stale=stale,
        )
