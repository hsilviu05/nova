"""Alerts as the app reads them."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str = Field(description="project_down or project_recovered.")
    project: str
    message: str
    created_at: datetime
    acknowledged_at: datetime | None = None


class AlertPage(BaseModel):
    items: list[AlertRead]
    unacknowledged: int = Field(description="Across every alert, not only this page.")


class AcknowledgedCount(BaseModel):
    acknowledged: int
