"""Tool failures, expressed in NOVA's own terms."""

from __future__ import annotations

from nova.core.errors import NovaError


class ToolError(NovaError):
    """A tool could not do what was asked."""

    status_code = 400
    code = "tool_failed"
    message = "The tool could not complete."


class ToolNotFoundError(ToolError):
    """No tool by that name is registered, or its group is turned off.

    One error for both, deliberately. Distinguishing them would tell a caller
    which capabilities exist but are disabled on this machine, which is
    exactly the map someone probing it would want.
    """

    status_code = 404
    code = "tool_not_found"
    message = "No such tool."


class ToolInputError(ToolError):
    """The arguments do not match the tool's schema."""

    status_code = 422
    code = "tool_invalid_input"
    message = "The tool arguments are invalid."


class ToolPermissionError(ToolError):
    """This tool is not allowed to run here.

    Raised when a model asks for something above what the chat path admits,
    and when a caller invokes a disabled capability.
    """

    status_code = 403
    code = "tool_not_permitted"
    message = "That action is not permitted."


class ToolConfirmationRequired(ToolError):
    """The tool would change something, and nobody has said yes yet.

    Carries the token the client presents to confirm. A 409 rather than a
    403: the request is well-formed and the caller may proceed, but not in
    one step.
    """

    status_code = 409
    code = "tool_confirmation_required"
    message = "This action needs your confirmation."


class ToolTimeoutError(ToolError):
    """The tool did not finish inside its budget."""

    status_code = 504
    code = "tool_timeout"
    message = "The tool took too long and was stopped."


class ToolUnavailableError(ToolError):
    """What the tool talks to is not reachable or not installed."""

    status_code = 503
    code = "tool_unavailable"
    message = "That tool is not available on this machine."
