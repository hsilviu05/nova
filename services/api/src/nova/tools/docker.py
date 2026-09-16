"""Tools for the containers running on this machine.

Spoken to through the ``docker`` CLI rather than the SDK or the socket. The
CLI is already installed wherever Docker is, it needs no dependency, and --
the reason that matters here -- it takes its arguments as argv, so a
container name is a name and never part of a command line.

Three read-only tools, and one destructive one that exists mainly to make the
confirmation machinery real. ``docker_remove_container`` is the worked
example of the rule in SECURITY.md: NOVA can propose it, and only a person
can run it.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from nova.core.config import ToolSettings
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
    narrow,
)
from nova.tools.process import run

# Container names and ids. Anchored and narrow: this is what Docker itself
# accepts, and matching it here means a name that could be mistaken for a
# flag ("--rm") never reaches argv at all.
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"

_LOG_LINES_MAX = 200


class _DockerTool(Tool):
    def __init__(self, settings: ToolSettings) -> None:
        self._settings = settings

    async def _docker(self, *args: str) -> Any:
        return await run(
            ["docker", *args],
            timeout_seconds=self._settings.command_timeout_seconds,
            max_output_bytes=self._settings.max_output_bytes,
        )


class DockerStatusTool(_DockerTool):
    """Is the daemon up, and how much is on it?"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="docker_status",
            description=(
                "Whether the Docker daemon is running, and how many containers "
                "and images exist. Use this before assuming Docker is available."
            ),
            group=ToolGroup.DOCKER,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        result = await self._docker(
            "info",
            "--format",
            "{{.ServerVersion}}|{{.Containers}}|{{.ContainersRunning}}|{{.Images}}",
        )
        if not result.ok:
            # The daemon being down is an ordinary answer to "is Docker
            # running", not a tool failure. Reported as a result so NOVA can
            # say so plainly instead of reporting a fault.
            return ToolResult(
                content="The Docker daemon is not responding. It is probably not running.",
                data={"running": False},
            )

        parts = result.stdout.strip().split("|")
        if len(parts) != 4:
            return ToolResult(content="Docker is running.", data={"running": True})

        version, total, active, images = parts
        return ToolResult(
            content=(
                f"Docker {version} is running: {active} of {total} containers up, {images} images."
            ),
            data={
                "running": True,
                "server_version": version,
                "containers": _int(total),
                "containers_running": _int(active),
                "images": _int(images),
            },
        )


class DockerContainersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_stopped: Annotated[
        bool,
        Field(default=False, description="Include containers that are not running."),
    ] = False


class DockerContainersTool(_DockerTool):
    """What is running."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="docker_containers",
            description=(
                "List Docker containers with their image, state, status and published ports."
            ),
            group=ToolGroup.DOCKER,
            permission=Permission.READ,
            input_model=DockerContainersInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, DockerContainersInput)

        args = ["ps", "--format", "{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}|{{.Ports}}"]
        if payload.include_stopped:
            args.append("--all")

        result = await self._docker(*args)
        if not result.ok:
            return ToolResult.failure(
                "Could not list containers; the Docker daemon may not be running.",
                code="docker_unavailable",
            )

        containers = [
            {
                "name": fields[0],
                "image": fields[1],
                "state": fields[2],
                "status": fields[3],
                "ports": fields[4],
            }
            for line in result.stdout.splitlines()
            if len(fields := line.split("|")) == 5
        ]
        if not containers:
            return ToolResult(content="No containers.", data={"containers": []})

        lines = [f"{c['name']}  {c['state']}  {c['image']}  ({c['status']})" for c in containers]
        return ToolResult(
            content="\n".join(lines),
            data={"containers": containers},
            truncated=result.truncated,
        )


class DockerLogsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=_NAME_PATTERN,
            description="Container name or id.",
        ),
    ]
    lines: Annotated[
        int,
        Field(default=50, ge=1, le=_LOG_LINES_MAX, description="How many trailing lines to read."),
    ] = 50


class DockerLogsTool(_DockerTool):
    """The tail of a container's log."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="docker_logs",
            description=(
                "The most recent log lines from one container. Use this to "
                "explain why a service is failing or restarting."
            ),
            group=ToolGroup.DOCKER,
            permission=Permission.READ,
            input_model=DockerLogsInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, DockerLogsInput)

        result = await self._docker(
            "logs", "--tail", str(payload.lines), "--timestamps", payload.container
        )
        # Containers write to stderr as a matter of course, so a non-zero
        # exit is the signal here, not the presence of stderr output.
        if not result.ok:
            return ToolResult.failure(
                f"Could not read logs for {payload.container}. "
                "Check the name with docker_containers.",
                code="docker_no_such_container",
            )

        body = result.output or "(no output)"
        return ToolResult(
            content=body,
            data={"container": payload.container, "lines": payload.lines},
            truncated=result.truncated,
        )


class DockerRemoveContainerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: Annotated[
        str,
        Field(
            min_length=1, max_length=128, pattern=_NAME_PATTERN, description="Container to remove."
        ),
    ]
    force: Annotated[
        bool,
        Field(default=False, description="Remove it even if it is running."),
    ] = False


class DockerRemoveContainerTool(_DockerTool):
    """Remove a container -- only ever after somebody has said yes.

    The one destructive tool NOVA ships with. It is here because the
    confirmation path needs something real to guard: a security mechanism
    with no user is a security mechanism nobody has tested.
    """

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="docker_remove_container",
            description=(
                "Remove a Docker container. This is destructive and always "
                "requires the person's explicit confirmation -- propose it and "
                "say what it would do, never assume it has happened."
            ),
            group=ToolGroup.DOCKER,
            permission=Permission.DESTRUCTIVE,
            input_model=DockerRemoveContainerInput,
            confirmation_prompt="Remove the Docker container “{container}”? This cannot be undone.",
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, DockerRemoveContainerInput)

        args = ["rm"]
        if payload.force:
            args.append("--force")
        args.append(payload.container)

        result = await self._docker(*args)
        if not result.ok:
            return ToolResult.failure(
                f"Could not remove {payload.container}: {result.output or 'unknown error'}",
                code="docker_remove_failed",
            )
        return ToolResult(
            content=f"Removed container {payload.container}.",
            data={"container": payload.container, "removed": True},
        )


def _int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def build_docker_tools(settings: ToolSettings) -> list[Tool]:
    return [
        DockerStatusTool(settings),
        DockerContainersTool(settings),
        DockerLogsTool(settings),
        DockerRemoveContainerTool(settings),
    ]
