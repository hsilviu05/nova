"""The git tools' parser, and what they do when git says no.

The companion integration test drives these against a real repository built
with real commits, which is the only way to know the parser agrees with the
git on this machine. What it cannot produce on demand is a merge conflict, a
detached HEAD, an upstream that is both ahead and behind, or a git that
failed -- so those are here, against recorded output.

``--porcelain=v2`` is used rather than the short format because the short
format is explicitly not guaranteed between git versions. v2 has five record
types and only two of them look alike, which is why the parsing is worth
testing on its own rather than only through a repository.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from nova.core.config import ToolSettings
from nova.tools.base import Permission, ToolContext
from nova.tools.errors import ToolError
from nova.tools.git import (
    GitDiffInput,
    GitLogInput,
    RepositoryInput,
    _parse_status,
    build_git_tools,
)
from nova.tools.process import CommandResult


class FakeGit:
    def __init__(self, *results: CommandResult) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append(argv)
        if not self._results:  # pragma: no cover - a mis-written test
            raise AssertionError(f"unexpected git call: {argv}")
        return self._results.pop(0)


def ok(stdout: str = "", *, truncated: bool = False) -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr="", truncated=truncated)


def failed(stderr: str) -> CommandResult:
    return CommandResult(exit_code=128, stdout="", stderr=stderr, truncated=False)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A directory that passes the "is this a repository" check.

    Only the marker is needed: git itself never runs in these tests.
    """
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def tools(tmp_path: Path) -> dict[str, Any]:
    settings = ToolSettings(workspace_roots=[str(tmp_path)])
    return {tool.spec.name: tool for tool in build_git_tools(settings)}


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(user_id=uuid.uuid4())


class TestParsingStatus:
    def test_the_branch_is_read_from_the_header(self) -> None:
        parsed = _parse_status("# branch.oid abc123\n# branch.head feature/terminal\n")

        assert parsed["branch"] == "feature/terminal"

    def test_a_branch_name_containing_spaces_survives(self) -> None:
        """git allows them. Splitting on every space would truncate one."""
        parsed = _parse_status("# branch.head wip the thing\n")

        assert parsed["branch"] == "wip the thing"

    def test_a_repository_with_no_branch_header_reports_none(self) -> None:
        """A detached HEAD, or a repository with no commits yet."""
        parsed = _parse_status("")

        assert parsed["branch"] is None
        assert parsed["files"] == []

    def test_being_ahead_and_behind_at_once_is_read_as_both(self) -> None:
        """The case that matters: a branch that needs a rebase before a push.

        Reporting only one side of it would be worse than reporting neither.
        """
        parsed = _parse_status("# branch.head main\n# branch.ab +3 -7\n")

        assert parsed["ahead"] == 3
        assert parsed["behind"] == 7

    def test_an_up_to_date_branch_is_zero_on_both_sides(self) -> None:
        parsed = _parse_status("# branch.head main\n# branch.ab +0 -0\n")

        assert (parsed["ahead"], parsed["behind"]) == (0, 0)

    def test_an_ordinary_change_gives_its_status_and_path(self) -> None:
        line = "1 .M N... 100644 100644 100644 abc def README.md\n"

        parsed = _parse_status(line)

        assert parsed["files"] == [{"status": ".M", "path": "README.md"}]

    def test_a_rename_reports_the_new_name_not_the_pair(self) -> None:
        """A "2 " record's path field is "new\\told". Reporting it whole
        would print a tab and both names into a chat bubble."""
        line = "2 R. N... 100644 100644 100644 abc def R100 new.py\told.py\n"

        parsed = _parse_status(line)

        assert parsed["files"] == [{"status": "R.", "path": "new.py"}]

    def test_an_untracked_file_is_listed_as_such(self) -> None:
        parsed = _parse_status("? scratch.txt\n")

        assert parsed["files"] == [{"status": "??", "path": "scratch.txt"}]

    def test_an_unmerged_file_is_listed_as_a_conflict(self) -> None:
        """The record type that only appears mid-conflict, which a real
        fixture repository would have to be deliberately broken to produce."""
        line = "u UU N... 100644 100644 100644 100644 aaa bbb ccc conflicted.py\n"

        parsed = _parse_status(line)

        assert parsed["files"] == [{"status": "UU", "path": "conflicted.py"}]

    @pytest.mark.parametrize(
        "line",
        [
            "1 .M N... 100644 README.md",  # a truncated ordinary record
            "u UU N... 100644 conflicted.py",  # a truncated unmerged record
            "! ignored.txt",  # a record type this does not read
            "# branch.upstream origin/main",
            "",
        ],
    )
    def test_a_record_that_does_not_parse_costs_one_row_not_the_answer(self, line: str) -> None:
        """A future git adding a field should not make git_status fail."""
        parsed = _parse_status(f"# branch.head main\n{line}\n? real.txt\n")

        assert parsed["branch"] == "main"
        assert {"status": "??", "path": "real.txt"} in parsed["files"]
        assert len(parsed["files"]) == 1

    def test_everything_at_once(self) -> None:
        parsed = _parse_status(
            "# branch.oid abc\n"
            "# branch.head main\n"
            "# branch.upstream origin/main\n"
            "# branch.ab +1 -2\n"
            "1 .M N... 100644 100644 100644 a b changed.py\n"
            "1 M. N... 100644 100644 100644 a b staged.py\n"
            "2 R. N... 100644 100644 100644 a b R100 new.py\told.py\n"
            "u UU N... 1 2 3 4 a b c conflicted.py\n"
            "? untracked.txt\n"
        )

        assert parsed["branch"] == "main"
        assert (parsed["ahead"], parsed["behind"]) == (1, 2)
        assert [f["path"] for f in parsed["files"]] == [
            "changed.py",
            "staged.py",
            "new.py",
            "conflicted.py",
            "untracked.txt",
        ]


class TestStatusTool:
    async def test_the_upstream_position_reaches_the_headline(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "nova.tools.git.run",
            FakeGit(ok("# branch.head main\n# branch.ab +2 -1\n")),
        )

        result = await tools["git_status"].execute(RepositoryInput(path=str(repository)), context)

        assert "(2 ahead, 1 behind)" in result.content
        assert "Working tree clean" in result.content

    async def test_a_git_that_failed_is_reported_rather_than_read_as_clean(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The dangerous failure mode.

        Empty output parses as "no changes", so a git that failed would
        otherwise be indistinguishable from a clean tree.
        """
        monkeypatch.setattr("nova.tools.git.run", FakeGit(failed("index.lock exists")))

        result = await tools["git_status"].execute(RepositoryInput(path=str(repository)), context)

        assert result.is_error is True
        assert "index.lock" in result.content

    async def test_a_directory_that_is_not_a_repository_says_so_plainly(
        self, tools: dict[str, Any], tmp_path: Path, context: ToolContext
    ) -> None:
        """git's own message is about discovery across filesystem
        boundaries, which is true and no help to anyone."""
        plain = tmp_path / "notarepo"
        plain.mkdir()

        with pytest.raises(ToolError) as caught:
            await tools["git_status"].execute(RepositoryInput(path=str(plain)), context)

        assert caught.value.code == "tool_not_a_repository"

    async def test_a_repository_outside_the_roots_is_refused(
        self, tools: dict[str, Any], context: ToolContext
    ) -> None:
        with pytest.raises(ToolError):
            await tools["git_status"].execute(RepositoryInput(path="/etc"), context)


class TestLogTool:
    async def test_a_repository_with_no_commits_says_so(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """git log in a fresh repository exits non-zero on some versions and
        prints nothing on others; both have to read as "no commits"."""
        monkeypatch.setattr("nova.tools.git.run", FakeGit(ok("")))

        result = await tools["git_log"].execute(GitLogInput(path=str(repository)), context)

        assert "no commits yet" in result.content
        assert result.data["commits"] == []

    async def test_a_failed_log_is_an_error(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nova.tools.git.run", FakeGit(failed("bad revision")))

        result = await tools["git_log"].execute(GitLogInput(path=str(repository)), context)

        assert result.is_error is True

    async def test_the_limit_is_passed_to_git_rather_than_applied_after(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reading the whole history and slicing it would make the bound on
        output meaningless on a large repository."""
        fake = FakeGit(ok("abc\x1fAda\x1f2 days ago\x1ffeat: a thing\n"))
        monkeypatch.setattr("nova.tools.git.run", fake)

        await tools["git_log"].execute(GitLogInput(path=str(repository), limit=3), context)

        assert "--max-count=3" in fake.calls[0]

    async def test_a_commit_subject_containing_the_separator_is_skipped(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A unit separator in a commit message would shift every field.

        Vanishingly unlikely, and dropping the row beats attributing the
        commit to the wrong person.
        """
        monkeypatch.setattr(
            "nova.tools.git.run",
            FakeGit(ok("abc\x1fAda\x1f2 days ago\x1fgood\ndef\x1fBob\x1fyesterday\n")),
        )

        result = await tools["git_log"].execute(GitLogInput(path=str(repository)), context)

        assert [c["sha"] for c in result.data["commits"]] == ["abc"]


class TestDiffTool:
    async def test_no_changes_says_which_kind_it_looked_for(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nova.tools.git.run", FakeGit(ok("   \n")))

        result = await tools["git_diff"].execute(GitDiffInput(path=str(repository)), context)

        assert "No unstaged changes" in result.content
        assert result.data["changed"] is False

    async def test_staged_changes_are_asked_for_explicitly(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = FakeGit(ok(" README.md | 2 +-\n 1 file changed\n"))
        monkeypatch.setattr("nova.tools.git.run", fake)

        result = await tools["git_diff"].execute(
            GitDiffInput(path=str(repository), staged=True), context
        )

        assert "--staged" in fake.calls[0]
        assert result.content.startswith("Staged changes in project:")
        assert result.data["staged"] is True

    async def test_a_failed_diff_is_an_error(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nova.tools.git.run", FakeGit(failed("not a valid object")))

        result = await tools["git_diff"].execute(GitDiffInput(path=str(repository)), context)

        assert result.is_error is True

    async def test_truncation_is_carried_through(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cut diffstat that looked complete would be read as the whole
        change set."""
        monkeypatch.setattr("nova.tools.git.run", FakeGit(ok("a.py | 1 +\n", truncated=True)))

        result = await tools["git_diff"].execute(GitDiffInput(path=str(repository)), context)

        assert result.truncated is True


class TestTheGroup:
    def test_nothing_here_can_change_a_working_tree(self, tools: dict[str, Any]) -> None:
        """Stated as a test because it is the group's whole design.

        A commit or a checkout would arrive as a WRITE or DESTRUCTIVE tool
        behind the confirmation gate, not as a flag on one of these.
        """
        assert all(tool.spec.permission is Permission.READ for tool in tools.values())

    async def test_every_invocation_names_the_repository_explicitly(
        self,
        tools: dict[str, Any],
        repository: Path,
        context: ToolContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``-C <repo>`` rather than relying on the working directory.

        git discovers upwards from wherever it starts, so a relative call
        could walk out of the resolved directory and report on a parent
        repository the roots were meant to exclude.
        """
        for name, payload in (
            ("git_status", RepositoryInput(path=str(repository))),
            ("git_log", GitLogInput(path=str(repository))),
            ("git_diff", GitDiffInput(path=str(repository))),
        ):
            fake = FakeGit(ok(""))
            monkeypatch.setattr("nova.tools.git.run", fake)

            await tools[name].execute(payload, context)

            assert fake.calls[0][:3] == ["git", "-C", str(repository.resolve())]
