"""NOVA's tool system.

What NOVA can do to the machine it runs on, and the rules about doing it.
Business logic imports the abstractions here, never a concrete tool: which
tools exist is a configuration question answered once at startup by
:func:`~nova.tools.registry.build_registry`.
"""

from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
)
from nova.tools.errors import (
    ToolConfirmationRequired,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolTimeoutError,
    ToolUnavailableError,
)
from nova.tools.registry import KnowledgeDependencies, ToolRegistry, build_registry

__all__ = [
    "KnowledgeDependencies",
    "Permission",
    "Tool",
    "ToolConfirmationRequired",
    "ToolContext",
    "ToolError",
    "ToolGroup",
    "ToolInputError",
    "ToolNotFoundError",
    "ToolPermissionError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolTimeoutError",
    "ToolUnavailableError",
    "build_registry",
]
