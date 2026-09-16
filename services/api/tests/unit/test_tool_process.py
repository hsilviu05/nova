"""Running programs, and the boundary that makes it survivable.

These run real processes. That is the point: a test that mocks
``create_subprocess_exec`` proves that the code calls a function, not that a
branch named ``; rm -rf ~`` is harmless, and the second is the claim that
matters.

Everything used here -- ``echo``, ``sh``, ``sleep``, ``env`` -- is in POSIX
and present on both macOS and the Linux runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.tools.errors import ToolError, ToolTimeoutError, ToolUnavailableError
from nova.tools.process import (
    child_environment,
    resolve_program,
    resolve_workspace_path,
    run,
)

pytestmark = pytest.mark.integration


class TestNoShell:
    async def test_arguments_are_never_interpreted(self) -> None:
        """The claim the whole design rests on.

        If a shell were involved anywhere, this would create the file. There
        is no shell, so ``;`` is one more character in an argument.
        """
        result = await run(
            ["echo", "hello; touch /tmp/nova-should-not-exist"],
            timeout_seconds=5,
            max_output_bytes=4096,
        )

        assert result.ok
        assert result.stdout.strip() == "hello; touch /tmp/nova-should-not-exist"
        assert not Path("/tmp/nova-should-not-exist").exists()  # noqa: S108

    async def test_substitution_is_not_performed(self) -> None:
        result = await run(
            ["echo", "$(whoami)", "`id`", "${HOME}"],
            timeout_seconds=5,
            max_output_bytes=4096,
        )

        assert "$(whoami)" in result.stdout
        assert "`id`" in result.stdout
        assert "${HOME}" in result.stdout

    async def test_globs_are_not_expanded(self) -> None:
        result = await run(["echo", "*"], timeout_seconds=5, max_output_bytes=4096)
        assert result.stdout.strip() == "*"


class TestEnvironment:
    def test_the_child_environment_is_built_not_inherited(self) -> None:
        """A deny-list over os.environ leaks whatever it has not heard of.

        The API process holds the database password, the JWT secret and any
        API tokens, and something does eventually print its environment.
        """
        env = child_environment()

        assert set(env) == {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "GIT_TERMINAL_PROMPT",
            "GIT_PAGER",
            "PAGER",
            "NO_COLOR",
        }

    async def test_a_child_cannot_read_nova_secrets(self, monkeypatch) -> None:
        """The concrete version of the rule above, proved by running one."""
        monkeypatch.setenv("NOVA_JWT__SECRET_KEY", "a-secret-that-must-not-leak")

        result = await run(["env"], timeout_seconds=5, max_output_bytes=8192)

        assert "a-secret-that-must-not-leak" not in result.stdout
        assert "NOVA_JWT__SECRET_KEY" not in result.stdout


class TestLimits:
    async def test_a_hung_command_is_killed(self) -> None:
        with pytest.raises(ToolTimeoutError):
            await run(["sleep", "30"], timeout_seconds=0.5, max_output_bytes=4096)

    async def test_output_is_capped(self) -> None:
        """``docker logs`` on a chatty container is unbounded by nature."""
        result = await run(
            ["sh", "-c", "yes nova | head -c 200000"],
            timeout_seconds=10,
            max_output_bytes=1024,
        )

        assert result.truncated
        assert len(result.stdout.encode()) <= 1024

    async def test_a_command_producing_a_lot_still_terminates(self) -> None:
        """Stopping the read outright would block the child on a full pipe
        until the timeout killed it, turning a chatty command into a slow
        one."""
        result = await run(
            ["sh", "-c", "yes nova | head -c 500000"],
            timeout_seconds=10,
            max_output_bytes=512,
        )

        assert result.exit_code == 0

    async def test_both_streams_are_drained_concurrently(self) -> None:
        """Reading one to completion first deadlocks whenever a command fills
        the other pipe's buffer, which ``git diff`` does reliably."""
        result = await run(
            ["sh", "-c", "yes err | head -c 100000 1>&2; echo done"],
            timeout_seconds=10,
            max_output_bytes=4096,
        )

        assert "done" in result.stdout


class TestPrograms:
    def test_a_missing_program_is_a_clear_message(self) -> None:
        with pytest.raises(ToolUnavailableError) as caught:
            resolve_program("definitely-not-installed-anywhere")

        assert caught.value.code == "tool_program_missing"

    def test_programs_resolve_on_the_safe_path(self) -> None:
        assert resolve_program("echo").endswith("/echo")


class TestWorkspaceBoundary:
    def test_a_path_inside_a_root_is_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "project").mkdir()

        resolved = resolve_workspace_path("project", [str(tmp_path)])

        assert resolved == (tmp_path / "project").resolve()

    def test_no_candidate_uses_the_first_root(self, tmp_path: Path) -> None:
        assert resolve_workspace_path(None, [str(tmp_path)]) == tmp_path.resolve()

    @pytest.mark.parametrize("candidate", ["/etc", "../..", "/", "~/../../etc/passwd"])
    def test_a_path_outside_every_root_is_refused(self, tmp_path: Path, candidate: str) -> None:
        """A model will invent a path, and output it was shown will suggest
        one. Neither can widen what NOVA is allowed to look at."""
        with pytest.raises(ToolError) as caught:
            resolve_workspace_path(candidate, [str(tmp_path)])

        assert caught.value.code == "tool_path_outside_workspace"

    def test_a_symlink_cannot_widen_the_boundary(self, tmp_path: Path) -> None:
        """Resolution happens before the containment check.

        Checking the literal path first and resolving afterwards would let a
        link inside a workspace point anywhere on the filesystem.
        """
        root = tmp_path / "workspace"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside)

        with pytest.raises(ToolError) as caught:
            resolve_workspace_path(str(root / "escape"), [str(root)])

        assert caught.value.code == "tool_path_outside_workspace"

    def test_with_no_roots_configured_nothing_is_reachable(self) -> None:
        """Not "everything", which is the dangerous reading of an empty list."""
        with pytest.raises(ToolError) as caught:
            resolve_workspace_path("/", [])

        assert caught.value.code == "tool_no_workspace"
