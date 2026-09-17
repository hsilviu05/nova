"""What the dashboard reads.

One response rather than six endpoints. The home screen needs all of it
before it can render anything useful, and separate requests would be separate
round trips over a phone's Wi-Fi and separate chances for the cards to
disagree about when they were taken.

Every section is optional-shaped rather than required: a NOVA with no
projects configured, no GitHub token, and Docker turned off is a correctly
configured NOVA, and its dashboard should say so rather than showing errors.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from nova.schemas.alert import AlertRead
from nova.schemas.health import DependencyStatus


class AIStatus(BaseModel):
    """Which model is answering, and whether it is actually there."""

    provider: str = Field(description="offline, ollama, openai_compatible, or anthropic.")
    model: str
    online: bool = Field(description="False when the provider could not be reached.")
    supports_tools: bool
    latency_ms: float | None = Field(
        default=None, description="Round trip of the reachability probe."
    )
    detail: str | None = Field(default=None, description="Why it is offline, when it is.")


class HostStatus(BaseModel):
    """The machine NOVA runs on."""

    hostname: str
    platform: str
    cpu_count: int
    load_per_core: float | None = None
    memory_percent_used: float | None = None
    disk_percent_used: float | None = None


class ProjectStatus(BaseModel):
    """A configured service, as the dashboard shows it."""

    name: str
    healthy: bool
    reachable: bool
    description: str | None = None
    status_code: int | None = None
    latency_ms: float | None = None
    dependencies: dict[str, bool] = Field(default_factory=dict)


class MemoryStatus(BaseModel):
    """How much NOVA knows."""

    total: int
    recent: list[str] = Field(
        default_factory=list, description="The most recently learned, newest first."
    )
    embedding_provider: str = Field(description="What embeds memories now.")
    stale: int = Field(
        default=0,
        description=(
            "Memories embedded by a different provider. Retrieval cannot see them until "
            "scripts/reembed_memories.py has run."
        ),
    )


class ActivityEntry(BaseModel):
    """One line of the recent-activity feed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    tool_group: str
    permission: str
    status: str
    initiated_by_model: bool
    confirmed: bool
    duration_ms: int | None
    error_code: str | None
    created_at: datetime


class ToolsStatus(BaseModel):
    """What NOVA is currently able to do."""

    count: int
    groups: list[str]
    shell_enabled: bool
    invocations_today: int
    failures_today: int


class AlertsStatus(BaseModel):
    """Whether NOVA is watching, and what it has noticed."""

    watching: bool = Field(description="False when the watcher is turned off.")
    interval_seconds: int
    unacknowledged: int
    latest: AlertRead | None = None


class SystemStatus(BaseModel):
    """Everything the home screen needs, in one response."""

    generated_at: datetime
    ai: AIStatus
    host: HostStatus
    dependencies: list[DependencyStatus]
    projects: list[ProjectStatus]
    memory: MemoryStatus
    tools: ToolsStatus
    alerts: AlertsStatus | None = None
    recent_activity: list[ActivityEntry]

    @property
    def healthy(self) -> bool:  # pragma: no cover - convenience for callers
        return all(d.healthy for d in self.dependencies) and self.ai.online


class ActivityPage(BaseModel):
    """A page of the audit log, as the app shows it."""

    items: list[ActivityEntry]
