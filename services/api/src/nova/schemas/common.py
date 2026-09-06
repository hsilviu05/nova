"""Shared response shapes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """The body of every non-2xx response."""

    code: str = Field(description="Stable, machine-readable error identifier.")
    message: str = Field(description="Human-readable explanation.")
    request_id: str | None = Field(
        default=None, description="Correlates this failure with server logs."
    )
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Envelope wrapping :class:`ErrorDetail`."""

    error: ErrorDetail
