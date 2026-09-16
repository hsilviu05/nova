"""The tools that talk to something else: Docker, GitHub, projects, shell.

Three different seams, chosen to match where the risk actually is:

* **Docker** is driven by faking ``run``. The command construction is already
  covered by ``test_tool_process``; what is unproven is whether the tool
  reads ``docker``'s output correctly, and that needs output, not a daemon.
* **GitHub and projects** are driven through ``httpx.MockTransport``, so the
  real adapter code parses real response *shapes*. A stub of NOVA's own
  making would only prove NOVA agrees with itself.
* **Shell** is driven for real, because its job is refusing things and a
  refusal that was mocked proves nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from nova.core.config import IntegrationSettings, ProjectTarget, ToolSettings
from nova.tools import docker as docker_module
from nova.tools.base import Permission, ToolContext
from nova.tools.docker import build_docker_tools
from nova.tools.errors import ToolError, ToolPermissionError
from nova.tools.github import build_github_tools
from nova.tools.projects import build_project_tools
from nova.tools.shell import build_shell_tools

pytestmark = pytest.mark.integration


def _context(*, by_model: bool = False) -> ToolContext:
    return ToolContext(user_id=uuid.uuid4(), initiated_by_model=by_model)


def _named(tools: list[Any]) -> dict[str, Any]:
    return {tool.spec.name: tool for tool in tools}


# -- Docker -------------------------------------------------------------------


@dataclass
class _FakeResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return (self.stdout or self.stderr).rstrip()


@pytest.fixture
def docker_output(monkeypatch: pytest.MonkeyPatch):
    """Replace ``run`` with something that returns scripted docker output."""
    scripted: dict[str, _FakeResult] = {}
    seen: list[list[str]] = []

    async def fake_run(argv: list[str], **kwargs: Any) -> _FakeResult:
        seen.append(argv)
        return scripted.get(argv[1], _FakeResult(exit_code=1, stderr="no such command"))

    monkeypatch.setattr(docker_module, "run", fake_run)
    return scripted, seen


class TestDockerTools:
    async def test_status_reads_the_daemon_summary(self, docker_output) -> None:
        scripted, _ = docker_output
        scripted["info"] = _FakeResult(exit_code=0, stdout="27.0.3|8|3|42\n")

        tool = _named(build_docker_tools(ToolSettings()))["docker_status"]
        result = await tool.execute(tool.spec.input_model(), _context())

        assert result.data == {
            "running": True,
            "server_version": "27.0.3",
            "containers": 8,
            "containers_running": 3,
            "images": 42,
        }

    async def test_a_stopped_daemon_is_an_answer_not_a_fault(self, docker_output) -> None:
        """ "Is Docker running?" has been correctly answered with "no"."""
        scripted, _ = docker_output
        scripted["info"] = _FakeResult(exit_code=1, stderr="Cannot connect to the Docker daemon")

        tool = _named(build_docker_tools(ToolSettings()))["docker_status"]
        result = await tool.execute(tool.spec.input_model(), _context())

        assert result.is_error is False
        assert result.data == {"running": False}

    async def test_containers_are_parsed_into_rows(self, docker_output) -> None:
        scripted, _ = docker_output
        scripted["ps"] = _FakeResult(
            exit_code=0,
            stdout=(
                "nova-api|nova-api:latest|running|Up 2 hours|0.0.0.0:8000->8000/tcp\n"
                "nova-postgres|pgvector/pgvector:pg17|running|Up 2 hours|5432/tcp\n"
            ),
        )

        tool = _named(build_docker_tools(ToolSettings()))["docker_containers"]
        result = await tool.execute(tool.spec.input_model(), _context())

        assert [row["name"] for row in result.data["containers"]] == [
            "nova-api",
            "nova-postgres",
        ]
        assert result.data["containers"][0]["ports"] == "0.0.0.0:8000->8000/tcp"

    async def test_stopped_containers_are_asked_for_explicitly(self, docker_output) -> None:
        scripted, seen = docker_output
        scripted["ps"] = _FakeResult(exit_code=0, stdout="")

        tool = _named(build_docker_tools(ToolSettings()))["docker_containers"]
        await tool.execute(tool.spec.input_model(include_stopped=True), _context())

        assert "--all" in seen[0]

    async def test_a_container_name_that_looks_like_a_flag_is_refused(self) -> None:
        """Inert without a shell, and refused anyway: an argument shaped like
        an injection is evidence of one."""
        tool = _named(build_docker_tools(ToolSettings()))["docker_logs"]

        with pytest.raises(Exception):  # noqa: B017 - pydantic's own error type
            tool.spec.input_model(container="--privileged")

    async def test_removing_a_container_is_destructive_and_says_what_it_would_do(
        self,
    ) -> None:
        tool = _named(build_docker_tools(ToolSettings()))["docker_remove_container"]

        assert tool.spec.permission is Permission.DESTRUCTIVE
        assert "nova-api" in tool.spec.describe_call({"container": "nova-api"})


# -- GitHub -------------------------------------------------------------------


def _github(handler) -> dict[str, Any]:
    return _named(
        build_github_tools(
            IntegrationSettings(github_token="ghp_fake"),  # type: ignore[arg-type]
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="https://api.github.com"
            ),
        )
    )


class TestGitHubTools:
    async def test_repositories_are_summarised(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "full_name": "hsilviu05/nova",
                        "private": False,
                        "language": "Python",
                        "pushed_at": "2026-09-16T10:00:00Z",
                        "open_issues_count": 3,
                        "description": "A personal AI terminal",
                    }
                ],
            )

        tool = _github(handler)["github_repositories"]
        result = await tool.execute(tool.spec.input_model(), _context())

        assert result.data["repositories"][0]["full_name"] == "hsilviu05/nova"
        assert "Python" in result.content

    async def test_the_token_is_sent_and_never_returned(self) -> None:
        """The credential goes out in a header and comes back in nothing."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=[])

        tool = _github(handler)["github_repositories"]
        result = await tool.execute(tool.spec.input_model(), _context())

        assert seen == ["Bearer ghp_fake"]
        assert "ghp_fake" not in result.content
        assert "ghp_fake" not in str(result.data)

    async def test_issues_exclude_pull_requests(self) -> None:
        """The issues endpoint returns both, distinguished only by a key.

        "How many open issues" gets the answer a person means.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"number": 1, "title": "A real issue", "user": {"login": "someone"}},
                    {
                        "number": 2,
                        "title": "A pull request",
                        "user": {"login": "someone"},
                        "pull_request": {"url": "..."},
                    },
                ],
            )

        tool = _github(handler)["github_issues"]
        result = await tool.execute(tool.spec.input_model(repository="hsilviu05/nova"), _context())

        assert [issue["number"] for issue in result.data["issues"]] == [1]

    async def test_a_commit_with_no_linked_account_still_has_an_author(self) -> None:
        """Web-UI commits and bot commits have no ``author``."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "abcdef1234567890",
                        "author": None,
                        "commit": {
                            "author": {"name": "A Bot", "date": "2026-09-16T10:00:00Z"},
                            "message": "chore: bump\n\nbody",
                        },
                    }
                ],
            )

        tool = _github(handler)["github_recent_commits"]
        result = await tool.execute(tool.spec.input_model(repository="hsilviu05/nova"), _context())

        commit = result.data["commits"][0]
        assert commit["author"] == "A Bot"
        # Only the subject, not the whole body.
        assert commit["subject"] == "chore: bump"

    async def test_a_rejected_token_does_not_leak_the_response_body(self) -> None:
        """GitHub echoes request context in errors, and a 401 body is not
        something to forward to a model."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials for ghp_fake"})

        tool = _github(handler)["github_repositories"]

        with pytest.raises(ToolError) as caught:
            await tool.execute(tool.spec.input_model(), _context())

        assert caught.value.code == "github_forbidden"
        assert "ghp_fake" not in caught.value.message

    async def test_an_unreachable_github_is_reported_plainly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        tool = _github(handler)["github_repositories"]

        with pytest.raises(ToolError) as caught:
            await tool.execute(tool.spec.input_model(), _context())

        assert caught.value.code == "github_unreachable"

    def test_a_repository_name_must_be_owner_slash_name(self) -> None:
        tools = _github(lambda request: httpx.Response(200, json=[]))

        with pytest.raises(Exception):  # noqa: B017 - pydantic's own error type
            tools["github_issues"].spec.input_model(repository="../../etc/passwd")


# -- Projects -----------------------------------------------------------------


def _projects(handler, targets: list[ProjectTarget]) -> dict[str, Any]:
    return _named(
        build_project_tools(
            IntegrationSettings(projects=targets),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )


SNAPWORTH = ProjectTarget(
    name="SnapWorth", base_url="http://127.0.0.1:9000", description="Valuation API"
)


class TestProjectTools:
    async def test_a_healthy_project_reports_its_dependencies(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ready": True,
                    "dependencies": [
                        {"name": "postgres", "healthy": True},
                        {"name": "redis", "healthy": True},
                    ],
                },
            )

        tool = _projects(handler, [SNAPWORTH])["project_health"]
        result = await tool.execute(tool.spec.input_model(project="SnapWorth"), _context())

        assert result.data["healthy"] is True
        assert result.data["dependencies"] == {"postgres": True, "redis": True}
        assert "postgres: ok" in result.content

    async def test_a_flat_health_body_is_understood_too(self) -> None:
        """Not every service publishes NOVA's shape."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"checks": {"database": "up", "cache": "down"}})

        tool = _projects(handler, [SNAPWORTH])["project_health"]
        result = await tool.execute(tool.spec.input_model(project="SnapWorth"), _context())

        assert result.data["dependencies"] == {"database": True, "cache": False}

    async def test_prose_is_not_mistaken_for_a_dependency_report(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="OK")

        tool = _projects(handler, [SNAPWORTH])["project_health"]
        result = await tool.execute(tool.spec.input_model(project="SnapWorth"), _context())

        assert result.data["healthy"] is True
        assert result.data["dependencies"] == {}

    async def test_an_unreachable_project_is_an_answer(self) -> None:
        """ "Is SnapWorth up" has been correctly answered with "no"."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        tool = _projects(handler, [SNAPWORTH])["project_health"]
        result = await tool.execute(tool.spec.input_model(project="SnapWorth"), _context())

        assert result.is_error is False
        assert result.data == {
            "project": "SnapWorth",
            "healthy": False,
            "reachable": False,
            "url": "http://127.0.0.1:9000/health",
        }

    async def test_the_model_chooses_a_project_by_name_not_by_url(self) -> None:
        """The security property of this tool.

        If a URL were an argument, an injection could aim NOVA's health check
        at any address reachable from the server -- a request forgery with
        NOVA's network position.
        """
        tool = _projects(lambda r: httpx.Response(200), [SNAPWORTH])["project_health"]

        assert set(tool.spec.input_schema["properties"]) == {"project"}

        with pytest.raises(ToolError) as caught:
            await tool.execute(
                tool.spec.input_model(project="http://169.254.169.254/latest/meta-data"),
                _context(),
            )
        assert caught.value.code == "project_unknown"

    async def test_an_unknown_project_names_the_ones_that_exist(self) -> None:
        tool = _projects(lambda r: httpx.Response(200), [SNAPWORTH])["project_health"]

        with pytest.raises(ToolError) as caught:
            await tool.execute(tool.spec.input_model(project="Nonesuch"), _context())

        assert "SnapWorth" in caught.value.message

    async def test_the_list_tool_names_what_nova_watches(self) -> None:
        tool = _projects(lambda r: httpx.Response(200), [SNAPWORTH])["project_list"]
        result = await tool.execute(tool.spec.input_model(), _context())

        assert result.data["projects"] == [{"name": "SnapWorth", "description": "Valuation API"}]


# -- Shell --------------------------------------------------------------------


def _shell(**overrides: Any):
    settings = ToolSettings(shell_enabled=True, shell_allowlist=["echo"], **overrides)
    return build_shell_tools(settings)[0]


class TestShellTool:
    async def test_an_allowlisted_program_runs(self) -> None:
        tool = _shell()

        result = await tool.execute(
            tool.spec.input_model(program="echo", arguments=["hello"]), _context()
        )

        assert result.content.strip() == "hello"
        assert result.data["exit_code"] == 0

    async def test_anything_not_on_the_allowlist_is_refused(self) -> None:
        tool = _shell()

        with pytest.raises(ToolPermissionError) as caught:
            await tool.execute(
                tool.spec.input_model(program="rm", arguments=["-rf", "/"]), _context()
            )

        assert caught.value.code == "shell_program_not_allowed"

    async def test_shell_metacharacters_in_an_argument_are_refused(self) -> None:
        """Inert -- there is no interpreter -- and refused anyway, so an
        attempt shows up in the audit log instead of quietly doing nothing."""
        tool = _shell()

        with pytest.raises(ToolPermissionError) as caught:
            await tool.execute(
                tool.spec.input_model(program="echo", arguments=["a; rm -rf ~"]), _context()
            )

        assert caught.value.code == "shell_argument_rejected"

    async def test_the_confirmation_shows_the_whole_command(self) -> None:
        """``git`` is harmless and ``git reset --hard`` is not; a prompt that
        hid the difference would be collecting a signature on a blank page."""
        tool = _shell()

        prompt = await tool.describe(
            tool.spec.input_model(program="echo", arguments=["--force", "everything"]),
            _context(),
        )

        assert prompt is not None
        assert "echo --force everything" in prompt

    def test_it_is_always_destructive(self) -> None:
        """NOVA cannot know an allowlisted program is harmless with the
        arguments it was just handed."""
        assert _shell().spec.permission is Permission.DESTRUCTIVE

    async def test_the_working_directory_is_confined(self, tmp_path: Any) -> None:
        tool = _shell(workspace_roots=[str(tmp_path)])

        with pytest.raises(ToolError) as caught:
            await tool.execute(
                tool.spec.input_model(program="echo", working_directory="/etc"), _context()
            )

        assert caught.value.code == "tool_path_outside_workspace"
