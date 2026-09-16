"""Tool payloads.

The permission level is exposed to the *app*, unlike to the model. The person
holding the phone is entitled to know which of these buttons will change
something before they press it; the model is not entitled to reason about how
to get past the gate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_TOOL_NAME = 64


class ToolRead(BaseModel):
    """One tool, as the app lists it."""

    name: str
    description: str
    group: str
    permission: Literal["read", "write", "destructive"]
    requires_confirmation: bool
    input_schema: dict[str, Any] = Field(
        description="JSON Schema for the arguments, for rendering a form."
    )


class ToolListResponse(BaseModel):
    """Everything NOVA can do on this machine."""

    items: list[ToolRead]
    shell_enabled: bool = Field(
        description="Whether arbitrary command execution is turned on. Usually false."
    )


class InvokeToolRequest(BaseModel):
    """Run a tool directly, from the app rather than through a conversation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_TOOL_NAME)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Present on the second call, after the person has seen what would
    # happen. Absent on the first, which is what makes the server answer with
    # a confirmation rather than an action.
    confirmation_token: str | None = Field(default=None, max_length=128)


class ToolResultRead(BaseModel):
    """What a tool produced."""

    tool: str
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    is_error: bool
    truncated: bool
    duration_ms: int
