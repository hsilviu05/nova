"""The system and git tools, against this machine and a real repository.

Not mocked. ``system_health`` reading the actual load average and ``git_log``
reading an actual repository is the only way to find out that the parsing
matches what the tools really emit -- and the parsers are the part most
likely to be subtly wrong, because ``git status --porcelain=v2`` has five
record types and only two of them look alike.

The repository is built here with real commits rather than checked in. A
fixture repository would go stale, and one built from the project's own
history would make the assertions depend on what somebody committed
yesterday.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.core.config import ToolSettings
from nova.tools.base import Permission, ToolContext
from nova.tools.errors import ToolError
from nova.tools.git import build_git_tools
from nova.tools.process import run
from nova.tools.system import build_system_tools

pytestmark = pytest.mark.integration


def _context() -> ToolContext:
    import uuid

    return ToolContext(user_id=uuid.uuid4())


async def _git(repository: Path, *args: str) -> None:
    result = await run(
        ["git", "-C", str(repository), *args], timeout_seconds=20, max_output_bytes=8192
    )
    if not result.ok:  # pragma: no cover - a broken fixture, not a finding
        raise AssertionError(f"git {' '.join(args)} failed: {result.output}")


@pytest.fixture
async def repository(tmp_path: Path) -> Path:
    """A small real repository with one commit and one uncommitted change."""
    root = tmp_path / "project"
    root.mkdir()

    await _git(root, "init", "--initial-branch=main")
    await _git(root, "config", "user.email", "tester@example.com")
    await _git(root, "config", "user.name", "Tester")

    (root / "README.md").write_text("# Project\n")
    await _git(root, "add", "README.md")
    await _git(root, "commit", "-m", "feat: the first commit")

    (root / "README.md").write_text("# Project\n\nNow with more words.\n")
    (root / "untracked.txt").write_text("hello\n")
    return root


def _tools(roots: list[str]):
    return {tool.spec.name: tool for tool in build_git_tools(ToolSettings(workspace_roots=roots))}


class TestSystemTools:
    def test_every_system_tool_is_read_only(self) -> None:
        """Nothing NOVA ships with can change the machine out of the box."""
        for tool in build_system_tools(ToolSettings()):
            assert tool.spec.permission is Permission.READ

    async def test_system_health_reports_a_verdict_and_the_numbers(self) -> None:
        tools = {tool.spec.name: tool for tool in build_system_tools(ToolSettings())}

        result = await tools["system_health"].execute(
            tools["system_health"].spec.input_model(), _context()
        )

        assert result.is_error is False
        assert result.data["cpu_count"] >= 1
        assert isinstance(result.data["healthy"], bool)
        # The text is what the model reads; it has to contain the facts.
        assert str(result.data["cpu_count"]) in result.content

    async def test_system_resources_reports_load(self) -> None:
        tools = {tool.spec.name: tool for tool in build_system_tools(ToolSettings())}

        result = await tools["system_resources"].execute(
            tools["system_resources"].spec.input_model(), _context()
        )

        assert "CPU" in result.content
        assert len(result.data["load_average"]) == 3

    async def test_running_processes_lists_something(self) -> None:
        tools = {tool.spec.name: tool for tool in build_system_tools(ToolSettings())}

        result = await tools["running_processes"].execute(
            tools["running_processes"].spec.input_model(), _context()
        )

        # Checked before reading `processes`, so a `ps` invocation that is
        # not portable fails with the message the tool produced rather than
        # a KeyError three lines later. This shipped once using BSD-only
        # flags and passed on macOS while returning nothing on Linux.
        assert not result.is_error, result.content
        assert result.data["processes"]
        assert all(isinstance(row["pid"], int) for row in result.data["processes"])

    async def test_running_processes_are_sorted_by_cpu(self) -> None:
        """Sorted in Python, because the flag for it is not portable.

        BSD spells it `-r` and procps spells it `--sort=-pcpu`; either one
        makes the tool return nothing at all on the other platform.
        """
        tools = {tool.spec.name: tool for tool in build_system_tools(ToolSettings())}

        result = await tools["running_processes"].execute(
            tools["running_processes"].spec.input_model(), _context()
        )

        usage = [float(row["cpu_percent"]) for row in result.data["processes"]]
        assert usage == sorted(usage, reverse=True)

    def test_the_process_parser_is_robust_to_what_ps_actually_prints(self) -> None:
        """Full paths on Linux, and a dash where a percentage cannot be read.

        Both appear on real machines, and a sort that raised on the dash
        would lose the whole listing rather than one row.
        """
        from nova.tools.system import _parse_ps

        rows = _parse_ps(
            "  PID  %CPU %MEM COMM\n"
            "    1   0.7  0.1 /sbin/launchd\n"
            "  530     -  0.1 /usr/libexec/logd\n"
            "  777  12.5  0.3 postgres\n"
            "garbage line\n",
            limit=10,
        )

        assert [row["name"] for row in rows] == ["postgres", "launchd", "logd"]
        assert rows[0]["pid"] == 777

    async def test_disk_usage_is_confined_to_the_workspace(self, tmp_path: Path) -> None:
        tools = {
            tool.spec.name: tool
            for tool in build_system_tools(ToolSettings(workspace_roots=[str(tmp_path)]))
        }
        tool = tools["disk_usage"]

        with pytest.raises(ToolError) as caught:
            await tool.execute(tool.spec.input_model(path="/etc"), _context())

        assert caught.value.code == "tool_path_outside_workspace"


class TestGitStatus:
    async def test_reports_the_branch_and_the_changes(self, repository: Path) -> None:
        tool = _tools([str(repository.parent)])["git_status"]

        result = await tool.execute(tool.spec.input_model(path=str(repository)), _context())

        assert result.data["branch"] == "main"
        paths = {entry["path"] for entry in result.data["files"]}
        assert paths == {"README.md", "untracked.txt"}

    async def test_distinguishes_untracked_from_modified(self, repository: Path) -> None:
        """Two different questions -- "what have I changed" and "what have I
        forgotten to add" -- and a status that merged them would answer
        neither."""
        tool = _tools([str(repository.parent)])["git_status"]

        result = await tool.execute(tool.spec.input_model(path=str(repository)), _context())
        by_path = {entry["path"]: entry["status"] for entry in result.data["files"]}

        assert by_path["untracked.txt"] == "??"
        assert by_path["README.md"] != "??"

    async def test_a_clean_tree_says_so(self, repository: Path) -> None:
        await _git(repository, "checkout", "--", "README.md")
        (repository / "untracked.txt").unlink()

        tool = _tools([str(repository.parent)])["git_status"]
        result = await tool.execute(tool.spec.input_model(path=str(repository)), _context())

        assert "clean" in result.content
        assert result.data["files"] == []

    async def test_a_directory_that_is_not_a_repository_says_which(self, tmp_path: Path) -> None:
        """git in a non-repository prints a message about filesystem
        boundaries, which is true and unhelpful."""
        tool = _tools([str(tmp_path)])["git_status"]

        with pytest.raises(ToolError) as caught:
            await tool.execute(tool.spec.input_model(), _context())

        assert caught.value.code == "tool_not_a_repository"

    async def test_a_repository_outside_the_workspace_is_unreachable(
        self, repository: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        tool = _tools([str(elsewhere)])["git_status"]

        with pytest.raises(ToolError) as caught:
            await tool.execute(tool.spec.input_model(path=str(repository)), _context())

        assert caught.value.code == "tool_path_outside_workspace"


class TestGitLog:
    async def test_lists_commits_newest_first(self, repository: Path) -> None:
        (repository / "second.txt").write_text("second\n")
        await _git(repository, "add", "second.txt")
        await _git(repository, "commit", "-m", "feat: the second commit")

        tool = _tools([str(repository.parent)])["git_log"]
        result = await tool.execute(
            tool.spec.input_model(path=str(repository), limit=10), _context()
        )

        subjects = [commit["subject"] for commit in result.data["commits"]]
        assert subjects == ["feat: the second commit", "feat: the first commit"]

    async def test_a_subject_containing_the_separator_still_parses(self, repository: Path) -> None:
        """The field separator is a unit separator, not a pipe or a tab.

        Commit subjects contain pipes and tabs routinely; they cannot contain
        U+001F.
        """
        await _git(repository, "add", "-A")
        await _git(repository, "commit", "-m", "fix: handle a|b and\tc properly")

        tool = _tools([str(repository.parent)])["git_log"]
        result = await tool.execute(tool.spec.input_model(path=str(repository)), _context())

        assert result.data["commits"][0]["subject"] == "fix: handle a|b and\tc properly"

    async def test_the_limit_is_honoured(self, repository: Path) -> None:
        for n in range(4):
            (repository / f"f{n}.txt").write_text("x\n")
            await _git(repository, "add", "-A")
            await _git(repository, "commit", "-m", f"chore: commit {n}")

        tool = _tools([str(repository.parent)])["git_log"]
        result = await tool.execute(
            tool.spec.input_model(path=str(repository), limit=2), _context()
        )

        assert len(result.data["commits"]) == 2


class TestGitDiff:
    async def test_summarises_unstaged_changes(self, repository: Path) -> None:
        """A stat summary, not the patch: a full diff is unbounded and is the
        easiest way there is to fill a context window."""
        tool = _tools([str(repository.parent)])["git_diff"]

        result = await tool.execute(tool.spec.input_model(path=str(repository)), _context())

        assert result.data["changed"] is True
        assert "README.md" in result.content
        # The added line itself is not in a stat summary.
        assert "Now with more words" not in result.content

    async def test_staged_changes_are_a_separate_question(self, repository: Path) -> None:
        await _git(repository, "add", "README.md")
        tool = _tools([str(repository.parent)])["git_diff"]

        staged = await tool.execute(
            tool.spec.input_model(path=str(repository), staged=True), _context()
        )
        unstaged = await tool.execute(
            tool.spec.input_model(path=str(repository), staged=False), _context()
        )

        assert staged.data["changed"] is True
        assert unstaged.data["changed"] is False

    async def test_no_changes_says_so_plainly(self, repository: Path) -> None:
        await _git(repository, "checkout", "--", "README.md")
        tool = _tools([str(repository.parent)])["git_diff"]

        result = await tool.execute(tool.spec.input_model(path=str(repository)), _context())

        assert result.data["changed"] is False
        assert "No unstaged changes" in result.content
