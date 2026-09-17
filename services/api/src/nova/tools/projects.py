"""Tools that check the services NOVA has been told about.

SnapWorth, AIInterviewCoach, and whatever comes next are not special-cased.
They are entries in ``NOVA_INTEGRATIONS__PROJECTS``, and these two tools work
for all of them. A per-project tool would mean a code change for every new
side project, which is exactly the kind of thing that stops getting done.

The model chooses a project by *name*, never by URL. That is deliberate: if a
URL were an argument, a prompt injection could aim NOVA's health check at any
address reachable from the server, which is a request forgery with NOVA's
network position. The set of addresses is fixed in configuration, and the
model can only pick from it.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from nova.core.config import IntegrationSettings, ProjectTarget
from nova.core.logging import get_logger
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
    narrow,
)
from nova.tools.errors import ToolError

logger = get_logger(__name__)

# A health body larger than this is not a health body.
_MAX_HEALTH_BODY = 4096


class _ProjectTool(Tool):
    def __init__(self, settings: IntegrationSettings, client: httpx.AsyncClient) -> None:
        self._projects = {project.name.lower(): project for project in settings.projects}
        self._client = client

    @property
    def _names(self) -> list[str]:
        return sorted(project.name for project in self._projects.values())

    def _lookup(self, name: str) -> ProjectTarget:
        project = self._projects.get(name.strip().lower())
        if project is None:
            raise ToolError(
                f"NOVA does not know a project called {name!r}. "
                f"Configured projects: {', '.join(self._names) or 'none'}.",
                code="project_unknown",
            )
        return project


class ProjectListTool(_ProjectTool):
    """Which projects NOVA has been told about."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="project_list",
            description=(
                "The projects and services NOVA is configured to monitor, by "
                "name. Use this when unsure what a project is called before "
                "checking its health."
            ),
            group=ToolGroup.PROJECTS,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        projects = [
            {"name": project.name, "description": project.description}
            for project in self._projects.values()
        ]
        if not projects:
            return ToolResult(
                content="No projects are configured for NOVA to monitor.",
                data={"projects": []},
            )

        lines = [
            f"{p['name']}" + (f" — {p['description']}" if p["description"] else "")
            for p in projects
        ]
        # Says what it did *not* do. A small model asked "is X up" reaches for
        # this tool first -- the description invites it to -- and then answers
        # from the description it just read, which is a guess dressed as a
        # status. Naming the tool that actually checks is what turns the
        # listing into a step rather than an answer.
        lines.append(
            "\nThese are names and descriptions only; nothing here has been "
            "checked. Call project_health with one of these names to find out "
            "whether it is actually up."
        )
        return ToolResult(content="\n".join(lines), data={"projects": projects})


class ProjectHealthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: Annotated[
        str,
        Field(
            min_length=1,
            max_length=48,
            description="Configured project name, e.g. 'SnapWorth'. Use project_list if unsure.",
        ),
    ]


class ProjectHealthTool(_ProjectTool):
    """Is a configured service up, and are its dependencies?"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="project_health",
            description=(
                "Check whether a configured project's API is healthy, including "
                "its own dependency report (database, cache, and so on) when it "
                "publishes one. Use this to answer 'is X up' or 'check X'."
            ),
            group=ToolGroup.PROJECTS,
            permission=Permission.READ,
            input_model=ProjectHealthInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, ProjectHealthInput)
        project = self._lookup(payload.project)

        url = f"{project.base_url}{project.health_path}"
        # Timed here rather than read off ``response.elapsed``: what the
        # dashboard reports is NOVA's own round trip, and ``elapsed`` is only
        # populated once a response has been read, which is a property of
        # httpx rather than of the check.
        started = time.perf_counter()
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.info("project_unreachable", project=project.name, error=type(exc).__name__)
            # Unreachable is an answer, not a tool failure: "is SnapWorth up"
            # has been correctly answered with "no".
            return ToolResult(
                content=f"{project.name} is not responding at {project.base_url}.",
                data={
                    "project": project.name,
                    "healthy": False,
                    "reachable": False,
                    "url": url,
                },
            )

        healthy = response.is_success
        dependencies = _dependencies(response)

        summary = (
            f"{project.name} answered {response.status_code}"
            f" ({'healthy' if healthy else 'unhealthy'})."
        )
        if dependencies:
            summary += " " + ", ".join(
                f"{name}: {'ok' if ok else 'failing'}" for name, ok in dependencies.items()
            )

        return ToolResult(
            content=summary,
            data={
                "project": project.name,
                "healthy": healthy,
                "reachable": True,
                "status_code": response.status_code,
                "url": url,
                "dependencies": dependencies,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )


def _dependencies(response: httpx.Response) -> dict[str, bool]:
    """Read a dependency report out of a health body, if there is one.

    Understands NOVA's own readiness shape and the two or three common
    variants, and gives up quietly otherwise. A health endpoint that returns
    prose is still a health endpoint -- the status code carries the verdict.
    """
    if len(response.content) > _MAX_HEALTH_BODY:
        return {}

    try:
        body = response.json()
    except ValueError:
        return {}

    if not isinstance(body, dict):
        return {}

    # NOVA's own: {"dependencies": [{"name": ..., "healthy": ...}, ...]}
    listed = body.get("dependencies")
    if isinstance(listed, list):
        return {
            str(item["name"]): bool(item.get("healthy"))
            for item in listed
            if isinstance(item, dict) and "name" in item
        }

    # Flat maps: {"database": "ok", "redis": "up"} or {"database": true}
    if isinstance(listed, dict):
        return {str(name): _is_ok(value) for name, value in listed.items()}

    for key in ("checks", "services", "components"):
        nested = body.get(key)
        if isinstance(nested, dict):
            return {str(name): _is_ok(value) for name, value in nested.items()}

    return {}


def _is_ok(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return _is_ok(value.get("status", value.get("healthy")))
    return str(value).lower() in {"ok", "up", "healthy", "pass", "true"}


def build_project_tools(
    settings: IntegrationSettings, *, http_client: httpx.AsyncClient | None = None
) -> list[Tool]:
    # Injectable for the same reason the GitHub client is: the translation
    # from a health body to a verdict is the part worth testing, and it
    # should be tested against real response shapes rather than a stub of
    # NOVA's own making.
    client = http_client or httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        # No redirect following: a health check that lands somewhere else is
        # not a health check, and a redirect is a way to reach an address
        # that is not in the configured set.
        follow_redirects=False,
    )
    return [ProjectListTool(settings, client), ProjectHealthTool(settings, client)]
