"""Health and readiness payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness. Answered without touching any dependency."""

    status: Literal["ok"] = "ok"
    version: str
    environment: str


class DependencyStatus(BaseModel):
    """Result of probing one downstream dependency."""

    name: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness. ``ready`` is false when any dependency is unreachable."""

    ready: bool
    dependencies: list[DependencyStatus] = Field(default_factory=list)
