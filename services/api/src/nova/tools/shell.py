"""Running a command, for the case where nothing else will do.

This tool is off. Turning it on is two settings, both of which have to be set
deliberately, and even then it runs only programs named one at a time in an
allowlist. That is not caution for its own sake: a general shell reachable
from a chat message is a remote code execution vulnerability with a friendly
interface, and every layer below -- argv arrays, a stripped environment, a
timeout -- exists to bound what a mistake costs, not to make an unbounded
capability safe.

What is enforced here, in order:

* **The program is on the allowlist.** Matched against the first argument
  exactly, before anything is resolved or spawned.
* **No shell.** Arguments go to ``execve`` as a list. There is no
  interpreter, so no globbing, no substitution, no pipes, no ``;``.
* **No shell metacharacters even so.** An argument containing one is
  rejected rather than passed through. It would be inert -- there is nothing
  to interpret it -- but an argument shaped like an injection attempt is
  evidence of an injection attempt, and refusing it is how that becomes
  visible in the audit log instead of succeeding quietly at doing nothing.
* **Confirmation.** ``DESTRUCTIVE``, always, whatever the command. NOVA
  cannot know that an allowlisted program is harmless with the arguments it
  was just handed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from nova.core.config import ToolSettings
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
from nova.tools.errors import ToolPermissionError
from nova.tools.process import resolve_workspace_path, run

logger = get_logger(__name__)

# Rejected in any argument. Inert without a shell, and refused anyway -- see
# the module docstring.
_METACHARACTERS = frozenset(";|&$`><\n\r\\\0")

_MAX_ARGUMENTS = 16


class ShellCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            description="The program to run. Must be one NOVA is configured to allow.",
        ),
    ]
    arguments: Annotated[
        list[str],
        Field(
            default_factory=list,
            max_length=_MAX_ARGUMENTS,
            description="Arguments, one per element. Not a command line: nothing is parsed.",
        ),
    ]
    working_directory: Annotated[
        str | None,
        Field(
            default=None,
            max_length=512,
            description="Must be inside a directory NOVA is configured to look at.",
        ),
    ] = None


class ShellCommandTool(Tool):
    """Run one allowlisted program."""

    def __init__(self, settings: ToolSettings) -> None:
        self._settings = settings
        # Normalised once. A trailing space in an environment variable is
        # otherwise the kind of thing that makes an allowlist silently empty.
        self._allowed = {entry.strip() for entry in settings.shell_allowlist if entry.strip()}

    @property
    def spec(self) -> ToolSpec:
        allowed = ", ".join(sorted(self._allowed)) or "nothing"
        return ToolSpec(
            name="execute_shell_command",
            description=(
                "Run one specific program with arguments. Only these are "
                f"permitted: {allowed}. Every call needs the person's explicit "
                "confirmation before it runs, so propose it and say what it "
                "would do rather than assuming it has happened."
            ),
            group=ToolGroup.DEVELOPER,
            permission=Permission.DESTRUCTIVE,
            input_model=ShellCommandInput,
            confirmation_prompt="Run “{program}” on this machine?",
        )

    async def describe(self, arguments: BaseModel, context: ToolContext) -> str | None:
        """Show the whole command, not just the program name.

        The arguments are the part that matters. ``git`` is harmless and
        ``git reset --hard`` is not, and a prompt that hid the difference
        would be collecting a signature on a blank page.
        """
        payload = narrow(arguments, ShellCommandInput)
        rendered = " ".join([payload.program, *payload.arguments])
        return f"Run this on your machine?\n\n{rendered}"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, ShellCommandInput)

        if payload.program not in self._allowed:
            logger.warning(
                "shell_program_refused",
                program=payload.program,
                by_model=context.initiated_by_model,
            )
            raise ToolPermissionError(
                f"{payload.program!r} is not on this machine's allowlist.",
                code="shell_program_not_allowed",
            )

        for argument in payload.arguments:
            if _METACHARACTERS.intersection(argument):
                logger.warning("shell_argument_refused", program=payload.program)
                raise ToolPermissionError(
                    "An argument contains shell metacharacters. Arguments are passed "
                    "directly to the program, so shell syntax is never interpreted -- "
                    "pass each argument separately instead.",
                    code="shell_argument_rejected",
                )

        cwd = (
            resolve_workspace_path(payload.working_directory, self._settings.workspace_roots)
            if self._settings.workspace_roots
            else None
        )

        result = await run(
            [payload.program, *payload.arguments],
            cwd=cwd,
            timeout_seconds=self._settings.command_timeout_seconds,
            max_output_bytes=self._settings.max_output_bytes,
        )
        logger.info(
            "shell_command_ran",
            program=payload.program,
            exit_code=result.exit_code,
            by_model=context.initiated_by_model,
        )

        return ToolResult(
            content=result.output or f"(no output, exit {result.exit_code})",
            data={
                "program": payload.program,
                "arguments": payload.arguments,
                "exit_code": result.exit_code,
                "working_directory": str(cwd) if cwd else None,
            },
            is_error=not result.ok,
            truncated=result.truncated,
        )


def build_shell_tools(settings: ToolSettings) -> list[Tool]:
    """The shell tool, if there is anything it would be allowed to run.

    An empty allowlist with shell enabled registers nothing. A tool that can
    only ever refuse is worse than no tool: the model is offered a capability
    and spends turns discovering it does not work.
    """
    if not any(entry.strip() for entry in settings.shell_allowlist):
        logger.warning("shell_tool_skipped", reason="empty_allowlist")
        return []
    return [ShellCommandTool(settings)]
