"""The Docker tools, against a recorded ``docker`` CLI.

Not run against a real daemon. What these tools contain is argv construction
and the parsing of a ``--format`` template, and both are exercised better by
a fake that can produce a daemon which is down, a container which does not
exist, and output with a field missing -- none of which a healthy local
Docker will produce on demand.

The argv assertions are the security-relevant half: every container name a
model supplies has to arrive as one argument, never as text in a command
line, and never in a position where it could be read as a flag.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from nova.core.config import ToolSettings
from nova.tools.base import Permission, ToolContext
from nova.tools.docker import (
    DockerContainersInput,
    DockerLogsInput,
    DockerRemoveContainerInput,
    build_docker_tools,
)
from nova.tools.process import CommandResult


class FakeDocker:
    """Stands in for the ``docker`` CLI, and records how it was called."""

    def __init__(self, *results: CommandResult) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append(argv)
        if not self._results:  # pragma: no cover - a mis-written test, not a finding
            raise AssertionError(f"unexpected docker call: {argv}")
        return self._results.pop(0)


def ok(stdout: str = "", *, stderr: str = "", truncated: bool = False) -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, truncated=truncated)


def failed(stderr: str = "boom", *, code: int = 1) -> CommandResult:
    return CommandResult(exit_code=code, stdout="", stderr=stderr, truncated=False)


@pytest.fixture
def tools() -> dict[str, Any]:
    return {tool.spec.name: tool for tool in build_docker_tools(ToolSettings())}


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(user_id=uuid.uuid4())


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeDocker) -> None:
    monkeypatch.setattr("nova.tools.docker.run", fake)


class TestStatus:
    async def test_a_running_daemon_is_summarised(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeDocker(ok("27.1.1|9|4|31\n"))
        install(monkeypatch, fake)

        result = await tools["docker_status"].execute(DockerContainersInput(), context)

        assert result.data == {
            "running": True,
            "server_version": "27.1.1",
            "containers": 9,
            "containers_running": 4,
            "images": 31,
        }
        assert "4 of 9 containers up" in result.content

    async def test_a_daemon_that_is_down_is_a_fact_not_a_fault(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Docker is not running" is an answer.

        Reported as a successful result so NOVA says so plainly, rather than
        as a tool error, which reads to the model as "something broke".
        """
        install(monkeypatch, FakeDocker(failed("Cannot connect to the Docker daemon")))

        result = await tools["docker_status"].execute(DockerContainersInput(), context)

        assert result.is_error is False
        assert result.data == {"running": False}
        assert "not responding" in result.content

    async def test_unexpected_output_still_reports_that_it_is_up(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A newer Docker could change the template's output.

        The daemon answered, which is the thing actually being asked; the
        counts are a bonus and their absence is not worth an error.
        """
        install(monkeypatch, FakeDocker(ok("27.1.1|9\n")))

        result = await tools["docker_status"].execute(DockerContainersInput(), context)

        assert result.data == {"running": True}

    async def test_a_non_numeric_count_becomes_none_rather_than_raising(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(ok("27.1.1|<none>|4|31\n")))

        result = await tools["docker_status"].execute(DockerContainersInput(), context)

        assert result.data["containers"] is None
        assert result.data["containers_running"] == 4


class TestContainers:
    async def test_running_containers_are_listed(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeDocker(
            ok(
                "nova-db|postgres:17|running|Up 3 hours|0.0.0.0:5432->5432/tcp\n"
                "nova-redis|redis:7|running|Up 3 hours|\n"
            )
        )
        install(monkeypatch, fake)

        result = await tools["docker_containers"].execute(DockerContainersInput(), context)

        assert [c["name"] for c in result.data["containers"]] == ["nova-db", "nova-redis"]
        assert result.data["containers"][0]["ports"] == "0.0.0.0:5432->5432/tcp"
        assert "nova-db  running  postgres:17" in result.content
        # Without --all, because that was not asked for.
        assert "--all" not in fake.calls[0]

    async def test_stopped_containers_are_asked_for_explicitly(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeDocker(ok("old|alpine|exited|Exited (0) 2 days ago|\n"))
        install(monkeypatch, fake)

        await tools["docker_containers"].execute(
            DockerContainersInput(include_stopped=True), context
        )

        assert fake.calls[0][-1] == "--all"

    async def test_a_malformed_row_is_skipped_rather_than_guessed_at(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A container whose status contains a pipe would shift every field.

        Better to drop the row than to report an image called "Up 3 hours".
        """
        install(monkeypatch, FakeDocker(ok("good|alpine|running|Up|\nbroken|alpine|running\n")))

        result = await tools["docker_containers"].execute(DockerContainersInput(), context)

        assert [c["name"] for c in result.data["containers"]] == ["good"]

    async def test_no_containers_says_so(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(ok("")))

        result = await tools["docker_containers"].execute(DockerContainersInput(), context)

        assert result.content == "No containers."
        assert result.data == {"containers": []}

    async def test_a_dead_daemon_is_an_error_here(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(failed()))

        result = await tools["docker_containers"].execute(DockerContainersInput(), context)

        assert result.is_error is True
        assert result.data["error"] == "docker_unavailable"

    async def test_truncation_is_carried_through(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(ok("a|b|running|Up|\n", truncated=True)))

        result = await tools["docker_containers"].execute(DockerContainersInput(), context)

        assert result.truncated is True


class TestLogs:
    async def test_the_tail_is_returned_and_the_name_is_one_argument(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeDocker(ok("2026-09-17T10:00:00Z ready\n"))
        install(monkeypatch, fake)

        result = await tools["docker_logs"].execute(
            DockerLogsInput(container="nova-db", lines=10), context
        )

        assert "ready" in result.content
        assert fake.calls[0] == ["docker", "logs", "--tail", "10", "--timestamps", "nova-db"]

    async def test_a_container_with_no_output_says_so_rather_than_nothing(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(ok("")))

        result = await tools["docker_logs"].execute(DockerLogsInput(container="quiet"), context)

        assert result.content == "(no output)"

    async def test_stderr_alone_is_not_a_failure(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Containers log to stderr routinely; the exit code is the signal."""
        install(monkeypatch, FakeDocker(ok("", stderr="INFO listening on 5432\n")))

        result = await tools["docker_logs"].execute(DockerLogsInput(container="nova-db"), context)

        assert result.is_error is False
        assert "listening" in result.content

    async def test_an_unknown_container_points_at_the_tool_that_lists_them(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(failed("No such container: nope")))

        result = await tools["docker_logs"].execute(DockerLogsInput(container="nope"), context)

        assert result.is_error is True
        assert result.data["error"] == "docker_no_such_container"
        assert "docker_containers" in result.content

    @pytest.mark.parametrize(
        "name",
        [
            "--rm",
            "-f",
            "nova db",
            "nova;rm -rf /",
            "$(whoami)",
            "../etc",
            "",
            "a" * 129,
        ],
    )
    def test_a_name_that_could_be_mistaken_for_a_flag_is_refused(self, name: str) -> None:
        """Rejected at the schema, before argv is built.

        argv already means a name can never become a second command; this
        stops it becoming a *flag*, which argv does not protect against.
        """
        with pytest.raises(ValidationError):
            DockerLogsInput(container=name)

    @pytest.mark.parametrize("lines", [0, -1, 201, 10_000])
    def test_the_log_length_is_bounded(self, lines: int) -> None:
        with pytest.raises(ValidationError):
            DockerLogsInput(container="nova-db", lines=lines)

    def test_unknown_fields_are_refused(self) -> None:
        """extra="forbid" everywhere: a model that invents an argument gets
        told, rather than having it silently ignored."""
        with pytest.raises(ValidationError):
            DockerLogsInput(container="nova-db", follow=True)


class TestRemoval:
    def test_it_is_destructive_and_carries_a_prompt(self, tools: dict[str, Any]) -> None:
        spec = tools["docker_remove_container"].spec

        assert spec.permission is Permission.DESTRUCTIVE
        assert spec.permission.needs_confirmation is True
        assert spec.confirmation_prompt is not None
        # The prompt names the container, so the person confirming sees which
        # one rather than "a container".
        assert "{container}" in spec.confirmation_prompt

    async def test_removal_passes_the_name_as_one_argument(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeDocker(ok(""))
        install(monkeypatch, fake)

        result = await tools["docker_remove_container"].execute(
            DockerRemoveContainerInput(container="old-thing"), context
        )

        assert fake.calls[0] == ["docker", "rm", "old-thing"]
        assert result.data == {"container": "old-thing", "removed": True}

    async def test_force_is_a_flag_and_precedes_the_name(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order matters: the name has to be last so it is never parsed as
        the argument to a flag."""
        fake = FakeDocker(ok(""))
        install(monkeypatch, fake)

        await tools["docker_remove_container"].execute(
            DockerRemoveContainerInput(container="stuck", force=True), context
        )

        assert fake.calls[0] == ["docker", "rm", "--force", "stuck"]

    async def test_a_failed_removal_reports_what_docker_said(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(failed("container is running")))

        result = await tools["docker_remove_container"].execute(
            DockerRemoveContainerInput(container="busy"), context
        )

        assert result.is_error is True
        assert result.data["error"] == "docker_remove_failed"
        assert "container is running" in result.content

    async def test_a_failure_with_no_output_still_produces_a_sentence(
        self, tools: dict[str, Any], context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeDocker(failed("")))

        result = await tools["docker_remove_container"].execute(
            DockerRemoveContainerInput(container="busy"), context
        )

        assert "unknown error" in result.content


class TestTheGroup:
    def test_only_one_tool_is_destructive(self, tools: dict[str, Any]) -> None:
        destructive = [
            name for name, tool in tools.items() if tool.spec.permission is Permission.DESTRUCTIVE
        ]
        assert destructive == ["docker_remove_container"]

    def test_nothing_in_the_group_is_a_write(self, tools: dict[str, Any]) -> None:
        """WRITE is reserved for the knowledge group; anything here that
        changes the machine is DESTRUCTIVE and needs a person."""
        assert all(tool.spec.permission is not Permission.WRITE for tool in tools.values())
