"""Analytics payloads.

Every response carries its ``coverage`` block. A chart without one invites
the reader to treat two days of data the same as two months, and that is the
mistake this whole phase is built to avoid.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Every request is a scan over telemetry, so the window is capped. A bound
# rather than a closed set of allowed values: the bound is the property that
# actually matters, and an enum of four magic numbers would only mean clients
# could not ask for the fortnight they wanted.
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 90
DEFAULT_WINDOW_DAYS = 30


class CoverageRead(BaseModel):
    """How much evidence the numbers below rest on."""

    total_events: int
    distinct_days: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    # Whether there is enough here to say anything about habits. The app
    # shows a different screen when this is false, rather than charts of
    # three data points.
    is_sufficient: bool


class HourBucket(BaseModel):
    """One local hour of the day."""

    hour: int = Field(ge=0, le=23)
    count: int


class DayBucket(BaseModel):
    day: date
    count: int


class WeekdayHourBucket(BaseModel):
    """One cell of the week grid. 0 is Monday, matching Python."""

    weekday: int = Field(ge=0, le=6)
    hour: int = Field(ge=0, le=23)
    count: int


class BatteryPoint(BaseModel):
    recorded_at: datetime
    percent: int = Field(ge=0, le=100)


class EventTypeBucket(BaseModel):
    event_type: str
    count: int


class GapRead(BaseModel):
    """A silence between heartbeats."""

    started_at: datetime
    ended_at: datetime
    seconds: int


class AnalyticsRead(BaseModel):
    """Everything the insights screen plots."""

    window_days: int
    # The timezone every bucket below was computed in, echoed so a client
    # rendering "14:00" knows whose two o'clock it is.
    timezone: str
    coverage: CoverageRead

    presence_by_hour: list[HourBucket]
    presence_by_day: list[DayBucket]
    presence_by_weekday_hour: list[WeekdayHourBucket]
    battery: list[BatteryPoint]
    event_types: list[EventTypeBucket]
    gaps: list[GapRead]


class InsightRead(BaseModel):
    """One statement, with the evidence behind it."""

    kind: str
    headline: str
    detail: str
    confidence: Literal["low", "medium", "high"]
    sample_size: int
    days_observed: int


class InsightsRead(BaseModel):
    """What can be said, and what cannot be said yet."""

    window_days: int
    timezone: str
    coverage: CoverageRead
    insights: list[InsightRead]
    # Present when there is too little data to say anything. Explicit, so a
    # client shows "still getting to know you" rather than an empty list that
    # is indistinguishable from a bug.
    insufficient_reason: str | None = None
