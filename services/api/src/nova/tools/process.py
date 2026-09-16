"""Running programs, the only way NOVA is allowed to.

Every tool that touches a command goes through :func:`run`. That is not
convenience -- it is the whole security boundary for anything outside the API
process:

* **argv, never a string.** ``create_subprocess_exec`` takes a program and a
  list, and there is no shell to interpret it. A branch named
  ``; rm -rf ~`` is a branch with an unusual name, not a second command.
  This is why no tool in NOVA ever builds a command line by formatting.
* **no inherited environment.** The API process holds the database password,
  the JWT secret, and any API tokens. A child that inherited them could
  print them, and something does eventually print its environment. Children
  get a minimal environment built here instead.
* **a deadline.** A command that has not answered is killed, and its process
  group with it, so a hung tool cannot hold a chat turn open forever.
* **bounded output.** Read up to a cap rather than into memory without
  limit. ``docker logs`` on a chatty container is unbounded by nature.

Paths are resolved against the configured workspace roots before anything is
spawned, so a path the model invented -- or one assembled from output it was
shown -- cannot point somewhere outside them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from nova.core.logging import get_logger
from nova.tools.errors import ToolError, ToolTimeoutError, ToolUnavailableError

logger = get_logger(__name__)

# What a child process is given. PATH so programs resolve, HOME because git
# refuses to run without one, LANG so output is not locale-scrambled. Nothing
# else: notably no NOVA_* variable, which is where every secret lives.
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

# Read in chunks so a command that produces gigabytes is cut off rather than
# buffered. Comfortably larger than any tool's output cap.
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a finished command produced."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Both streams, with stderr labelled when there is anything in it."""
        if self.stderr.strip() and self.stdout.strip():
            return f"{self.stdout.rstrip()}\n[stderr]\n{self.stderr.rstrip()}"
        return (self.stdout or self.stderr).rstrip()


def child_environment() -> dict[str, str]:
    """The environment a tool's child process runs with.

    Built rather than filtered. A deny-list over ``os.environ`` leaks
    whatever it has not heard of yet, and the point is that a child sees
    nothing it was not given deliberately.
    """
    return {
        "PATH": _SAFE_PATH,
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108 - a fallback, not a write target
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        # Stops git from opening an editor, a pager, or a credential prompt
        # in a process with no terminal, all of which hang until the timeout.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "NO_COLOR": "1",
    }


def resolve_program(program: str) -> str:
    """Find ``program`` on the safe PATH.

    Resolved before spawning so "docker is not installed" is a clear,
    actionable message rather than a ``FileNotFoundError`` surfacing as an
    unexpected failure.
    """
    found = shutil.which(program, path=_SAFE_PATH)
    if found is None:
        raise ToolUnavailableError(
            f"{program} is not installed on this machine.", code="tool_program_missing"
        )
    return found


def resolve_workspace_path(candidate: str | None, roots: list[str]) -> Path:
    """Resolve ``candidate`` to a real path inside one of ``roots``.

    Symlinks are resolved *before* the containment check, so a link inside a
    workspace pointing at ``/`` does not widen the boundary. With no
    candidate, the first configured root is used, which is what makes
    "what's my git status" work without the model having to guess a path.

    Raises:
        ToolError: if no roots are configured, or the path escapes them.
    """
    if not roots:
        raise ToolError(
            "No project directories are configured, so NOVA has nothing to look at.",
            code="tool_no_workspace",
        )

    resolved_roots = [Path(root).expanduser().resolve() for root in roots]
    if candidate is None or not candidate.strip():
        return resolved_roots[0]

    target = Path(candidate).expanduser()
    if not target.is_absolute():
        # A relative path is relative to the first root, not to the API's
        # working directory, which is an implementation detail the caller
        # has no business depending on.
        target = resolved_roots[0] / target

    target = target.resolve()
    for root in resolved_roots:
        if target == root or root in target.parents:
            return target

    raise ToolError(
        f"{candidate} is outside the directories NOVA is allowed to look at.",
        code="tool_path_outside_workspace",
    )


async def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float,
    max_output_bytes: int,
) -> CommandResult:
    """Run a program and return what it produced.

    Raises:
        ToolUnavailableError: if the program is not installed.
        ToolTimeoutError: if it does not finish inside ``timeout_seconds``.
    """
    if not argv:
        raise ToolError("No command to run.", code="tool_empty_command")

    argv = [resolve_program(argv[0]), *argv[1:]]

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None,
        env=child_environment(),
        # Its own process group, so killing it on timeout takes the whole
        # tree. Without this, a command that spawned children leaves them
        # running after the parent is killed.
        start_new_session=True,
    )

    try:
        stdout, stderr, truncated = await asyncio.wait_for(
            _read_bounded(process, max_output_bytes), timeout=timeout_seconds
        )
    except TimeoutError:
        await _terminate(process)
        logger.warning("tool_command_timeout", program=argv[0], timeout=timeout_seconds)
        raise ToolTimeoutError(
            f"{Path(argv[0]).name} did not finish within {timeout_seconds:g}s and was stopped."
        ) from None

    return CommandResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
    )


async def _read_bounded(
    process: asyncio.subprocess.Process, max_bytes: int
) -> tuple[str, str, bool]:
    """Drain both pipes with a cap, then wait for the process.

    Both are read concurrently. Reading one to completion first deadlocks
    whenever a command fills the other pipe's buffer, which ``git diff`` on a
    large change does reliably.

    The streams are typed optional because ``Process`` also models the case
    where a pipe was not requested. Every call here requests both, so a
    missing one would be a bug in this module rather than a runtime
    condition -- it degrades to an empty stream rather than raising.
    """
    out, err = await asyncio.gather(
        _read_stream(process.stdout, max_bytes),
        _read_stream(process.stderr, max_bytes),
    )
    await process.wait()

    return (
        out[0].decode("utf-8", "replace"),
        err[0].decode("utf-8", "replace"),
        out[1] or err[1],
    )


async def _read_stream(stream: asyncio.StreamReader | None, max_bytes: int) -> tuple[bytes, bool]:
    """Read up to ``max_bytes``, then keep draining so the writer is not blocked.

    Stopping the read outright would leave the child blocked on a full pipe
    until the timeout killed it, turning a chatty command into a slow one.
    """
    if stream is None:  # pragma: no cover - both pipes are always requested
        return b"", False

    buffer = bytearray()
    truncated = False

    while chunk := await stream.read(_READ_CHUNK):
        if len(buffer) < max_bytes:
            buffer.extend(chunk[: max_bytes - len(buffer)])
        if len(buffer) >= max_bytes:
            truncated = True

    return bytes(buffer), truncated


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill a process group that has run out of time.

    SIGKILL rather than SIGTERM: this path is only reached after the command
    has already had its whole budget, and a well-behaved shutdown it might
    attempt would need time that has just run out.
    """
    if process.returncode is not None:
        return
    # pragma: no cover on the suppression -- it fires when the process
    # exited between the check above and the signal, which is a race the
    # tests cannot schedule.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), 9)
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2)
