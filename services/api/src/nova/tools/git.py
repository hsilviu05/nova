"""Tools that read the local repositories.

Every one of these is a read. None of them commits, pushes, checks out, or
resets: NOVA reporting on a working tree is useful, and NOVA changing one
while someone is working in it is a way to lose an afternoon. If that ever
changes, it changes as a ``WRITE`` tool behind the confirmation gate, not as
a flag on one of these.

Paths are resolved through :func:`~nova.tools.process.resolve_workspace_path`
before anything runs, so a repository outside the configured roots cannot be
reached even by absolute path.
"""

from __future__ import annotations

from pathlib import Path
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
from nova.tools.errors import ToolError
from nova.tools.process import resolve_workspace_path, run

_PATH_FIELD = Field(
    default=None,
    max_length=512,
    description=(
        "Repository directory. Must be inside a directory NOVA is configured "
        "to look at; omit to use the default project."
    ),
)


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Annotated[str | None, _PATH_FIELD] = None


class GitLogInput(RepositoryInput):
    limit: Annotated[
        int,
        Field(default=10, ge=1, le=50, description="How many commits to list."),
    ] = 10


class GitDiffInput(RepositoryInput):
    staged: Annotated[
        bool,
        Field(default=False, description="Show staged changes instead of unstaged ones."),
    ] = False


class _GitTool(Tool):
    def __init__(self, settings: ToolSettings) -> None:
        self._settings = settings

    async def _git(self, repository: Path, *args: str) -> Any:
        return await run(
            ["git", "-C", str(repository), *args],
            cwd=repository,
            timeout_seconds=self._settings.command_timeout_seconds,
            max_output_bytes=self._settings.max_output_bytes,
        )

    def _repository(self, path: str | None) -> Path:
        """Resolve and verify that the target is actually a repository.

        Checked here rather than left to git so the failure says what is
        wrong. ``git`` in a non-repository prints a message about discovery
        across filesystem boundaries, which is true and unhelpful.
        """
        resolved = resolve_workspace_path(path, self._settings.workspace_roots)
        if not (resolved / ".git").exists():
            raise ToolError(f"{resolved} is not a git repository.", code="tool_not_a_repository")
        return resolved


class GitStatusTool(_GitTool):
    """Branch, upstream position, and what has changed."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_status",
            description=(
                "The state of a local repository: current branch, how far it is "
                "ahead or behind its upstream, and which files are modified, "
                "staged, or untracked."
            ),
            group=ToolGroup.GIT,
            permission=Permission.READ,
            input_model=RepositoryInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, RepositoryInput)
        repository = self._repository(payload.path)

        # Porcelain v2 because it is the documented stable format; the v1
        # short format is explicitly not guaranteed between versions.
        result = await self._git(repository, "status", "--porcelain=v2", "--branch")
        if not result.ok:
            return ToolResult.failure(f"git status failed: {result.output}")

        parsed = _parse_status(result.stdout)
        parsed["path"] = str(repository)

        headline = f"{repository.name} on {parsed['branch'] or 'an unknown branch'}"
        if parsed["ahead"] or parsed["behind"]:
            headline += f" ({parsed['ahead']} ahead, {parsed['behind']} behind)"

        if not parsed["files"]:
            return ToolResult(content=f"{headline}. Working tree clean.", data=parsed)

        listing = "\n".join(f"  {entry['status']}  {entry['path']}" for entry in parsed["files"])
        return ToolResult(
            content=f"{headline}. {len(parsed['files'])} changed:\n{listing}",
            data=parsed,
            truncated=result.truncated,
        )


class GitLogTool(_GitTool):
    """Recent commits."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_log",
            description=(
                "Recent commits in a local repository, newest first, with "
                "author, relative date and subject. Use this to answer what "
                "was worked on recently."
            ),
            group=ToolGroup.GIT,
            permission=Permission.READ,
            input_model=GitLogInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, GitLogInput)
        repository = self._repository(payload.path)

        # A unit separator between fields: it cannot occur in a commit
        # subject, while a pipe or a tab can and does.
        result = await self._git(
            repository,
            "log",
            f"--max-count={payload.limit}",
            "--date=relative",
            "--format=%h\x1f%an\x1f%ad\x1f%s",
        )
        if not result.ok:
            return ToolResult.failure(f"git log failed: {result.output}")

        commits = [
            {"sha": f[0], "author": f[1], "date": f[2], "subject": f[3]}
            for line in result.stdout.splitlines()
            if len(f := line.split("\x1f")) == 4
        ]
        if not commits:
            return ToolResult(
                content=f"{repository.name} has no commits yet.",
                data={"path": str(repository), "commits": []},
            )

        listing = "\n".join(
            f"{c['sha']}  {c['date']:<16}  {c['author']}  {c['subject']}" for c in commits
        )
        return ToolResult(
            content=f"{repository.name}, {len(commits)} most recent commits:\n{listing}",
            data={"path": str(repository), "commits": commits},
            truncated=result.truncated,
        )


class GitDiffTool(_GitTool):
    """What has actually changed.

    Returns a stat summary rather than the patch. A full diff is unbounded,
    it is the single easiest way to fill a context window, and the question
    behind "what changed" is almost always about which files -- if the patch
    itself is wanted, it is wanted in an editor, not read aloud.
    """

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_diff",
            description=(
                "A summary of uncommitted changes in a local repository: which "
                "files changed and how many lines, not the patch itself."
            ),
            group=ToolGroup.GIT,
            permission=Permission.READ,
            input_model=GitDiffInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, GitDiffInput)
        repository = self._repository(payload.path)

        args = ["diff", "--stat", "--no-color"]
        if payload.staged:
            args.append("--staged")

        result = await self._git(repository, *args)
        if not result.ok:
            return ToolResult.failure(f"git diff failed: {result.output}")

        scope = "staged" if payload.staged else "unstaged"
        body = result.stdout.strip()
        if not body:
            return ToolResult(
                content=f"No {scope} changes in {repository.name}.",
                data={"path": str(repository), "staged": payload.staged, "changed": False},
            )

        return ToolResult(
            content=f"{scope.capitalize()} changes in {repository.name}:\n{body}",
            data={"path": str(repository), "staged": payload.staged, "changed": True},
            truncated=result.truncated,
        )


def _parse_status(output: str) -> dict[str, Any]:
    """Read porcelain v2 into branch position and a file list.

    Only the fields NOVA reports are read. Lines that do not parse are
    skipped rather than failing the tool: an unrecognised record type in a
    future git should cost one row, not the whole answer.
    """
    branch: str | None = None
    ahead = behind = 0
    files: list[dict[str, str]] = []

    for line in output.splitlines():
        if line.startswith("# branch.head"):
            branch = line.split(" ", 2)[-1]
        elif line.startswith("# branch.ab"):
            for token in line.split()[2:]:
                if token.startswith("+"):
                    ahead = int(token[1:] or 0)
                elif token.startswith("-"):
                    behind = int(token[1:] or 0)
        elif line.startswith(("1 ", "2 ")):
            # "1 XY ..." ordinary change; "2 XY ..." a rename, whose path
            # field holds "new\told" -- only the new name is interesting.
            parts = line.split(" ", 8)
            if len(parts) >= 9:
                files.append({"status": parts[1], "path": parts[8].split("\t")[0]})
        elif line.startswith("? "):
            files.append({"status": "??", "path": line[2:]})
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            if len(parts) >= 11:
                files.append({"status": "UU", "path": parts[10]})

    return {"branch": branch, "ahead": ahead, "behind": behind, "files": files}


def build_git_tools(settings: ToolSettings) -> list[Tool]:
    return [GitStatusTool(settings), GitLogTool(settings), GitDiffTool(settings)]
