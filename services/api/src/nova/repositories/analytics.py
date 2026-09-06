"""Telemetry aggregation.

Every query here buckets by **local** time, not UTC. ``recorded_at AT TIME
ZONE :timezone`` hands the conversion to Postgres, which resolves daylight
saving from the tz database. Adding a fixed offset in Python instead would be
wrong for half the year everywhere that observes DST, and wrong in a way that
looks plausible: the numbers would still be numbers.

Aggregation runs in SQL rather than by loading rows and folding them in
Python. A month of heartbeats at the default interval is around ninety
thousand rows for a single device, and pulling those over the wire to count
them would be slow, memory-hungry, and no clearer to read.

There is no rollup table yet. At this volume the index on
``(device_id, recorded_at)`` covers these scans comfortably, and a
materialised rollup is a backfill, a refresh schedule, and a staleness
question in exchange for nothing measurable. See ADR 012.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Integer, and_, cast, func, select, text
from sqlalchemy.sql.elements import ColumnElement

from nova.models.device import DeviceTelemetry
from nova.repositories.base import BaseRepository

# The event a device emits when it notices someone. Presence analytics is
# built on this one type rather than on all telemetry, because heartbeats
# arrive whether anybody is there or not -- counting them would measure that
# the device is plugged in.
PRESENCE_EVENT = "person_detected"
HEARTBEAT_EVENT = "heartbeat"


@dataclass(frozen=True, slots=True)
class HourCount:
    """Events in one local hour of the day, 0-23."""

    hour: int
    count: int


@dataclass(frozen=True, slots=True)
class DayCount:
    """Events on one local calendar date."""

    day: date
    count: int


@dataclass(frozen=True, slots=True)
class WeekdayHourCount:
    """Events in one (weekday, hour) cell of the week.

    ``weekday`` is 0=Monday through 6=Sunday, matching Python's
    ``date.weekday()`` rather than Postgres's 0=Sunday, so the two halves of
    the system agree without a conversion at the boundary.
    """

    weekday: int
    hour: int
    count: int


@dataclass(frozen=True, slots=True)
class BatterySample:
    """One battery reading, in device time."""

    recorded_at: datetime
    percent: int


@dataclass(frozen=True, slots=True)
class EventTypeCount:
    event_type: str
    count: int


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much data there is, which decides what may be claimed from it."""

    total_events: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    # Distinct local calendar dates with at least one event. The honest
    # denominator for anything phrased as "usually": thirty thousand events
    # from a single afternoon is one day of evidence, not thirty thousand.
    distinct_days: int


@dataclass(frozen=True, slots=True)
class Gap:
    """A silence between consecutive heartbeats."""

    started_at: datetime
    ended_at: datetime
    seconds: int


class AnalyticsRepository(BaseRepository):
    """Aggregate queries over one device's telemetry."""

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _local(timezone: str) -> ColumnElement[datetime]:
        """``recorded_at`` rendered in the owner's local time.

        The timezone is bound as a parameter rather than interpolated. It
        reaches this from a user-supplied profile field, and an unvalidated
        name spliced into SQL would be an injection point -- the schema layer
        rejects unknown names, and this makes that not the only defence.
        """
        return DeviceTelemetry.recorded_at.op("AT TIME ZONE")(
            text(":timezone").bindparams(timezone=timezone)
        )

    def _scope(
        self,
        device_id: uuid.UUID,
        since: datetime,
        *,
        event_type: str | None,
    ) -> ColumnElement[bool]:
        conditions = [
            DeviceTelemetry.device_id == device_id,
            DeviceTelemetry.recorded_at >= since,
        ]
        if event_type is not None:
            conditions.append(DeviceTelemetry.event_type == event_type)
        return and_(*conditions)

    # -- aggregates -------------------------------------------------------

    async def by_hour(
        self,
        device_id: uuid.UUID,
        *,
        since: datetime,
        timezone: str,
        event_type: str | None = PRESENCE_EVENT,
    ) -> list[HourCount]:
        """Counts per local hour of day, every hour present.

        Hours with no events are returned as zero rather than omitted: a
        chart that silently drops empty hours reads as though the device was
        busy all night.
        """
        hour = cast(func.extract("hour", self._local(timezone)), Integer).label("hour")
        stmt = (
            select(hour, func.count().label("event_count"))
            .where(self._scope(device_id, since, event_type=event_type))
            .group_by(hour)
        )

        counts = {int(row.hour): int(row.event_count) for row in await self._session.execute(stmt)}
        return [HourCount(hour=h, count=counts.get(h, 0)) for h in range(24)]

    async def by_day(
        self,
        device_id: uuid.UUID,
        *,
        since: datetime,
        timezone: str,
        event_type: str | None = PRESENCE_EVENT,
    ) -> list[DayCount]:
        """Counts per local calendar date, oldest first.

        Days with no events are absent here, unlike hours: the caller knows
        the window and can fill it, and a device that was unplugged for a
        fortnight should not have a fortnight of fabricated zeroes attributed
        to it.
        """
        bucket = func.date(self._local(timezone)).label("day")
        stmt = (
            select(bucket, func.count().label("event_count"))
            .where(self._scope(device_id, since, event_type=event_type))
            .group_by(bucket)
            .order_by(bucket)
        )
        return [
            DayCount(day=row.day, count=int(row.event_count))
            for row in await self._session.execute(stmt)
        ]

    async def by_weekday_hour(
        self,
        device_id: uuid.UUID,
        *,
        since: datetime,
        timezone: str,
        event_type: str | None = PRESENCE_EVENT,
    ) -> list[WeekdayHourCount]:
        """The week as a 7x24 grid. Only non-empty cells are returned."""
        local = self._local(timezone)
        # Postgres DOW is 0=Sunday; Python's weekday() is 0=Monday. Convert
        # here so nothing downstream has to remember which convention it is
        # holding.
        weekday = cast((func.extract("dow", local) + 6) % 7, Integer).label("weekday")
        hour = cast(func.extract("hour", local), Integer).label("hour")

        stmt = (
            select(weekday, hour, func.count().label("event_count"))
            .where(self._scope(device_id, since, event_type=event_type))
            .group_by(weekday, hour)
            .order_by(weekday, hour)
        )
        return [
            WeekdayHourCount(
                weekday=int(row.weekday), hour=int(row.hour), count=int(row.event_count)
            )
            for row in await self._session.execute(stmt)
        ]

    async def battery_series(
        self, device_id: uuid.UUID, *, since: datetime, limit: int = 2000
    ) -> list[BatterySample]:
        """Battery readings oldest first, for trend and drain estimation.

        Ordered ascending because the consumer fits a line through them, and
        a reversed series would produce a slope of the right size and the
        wrong sign.
        """
        stmt = (
            select(DeviceTelemetry.recorded_at, DeviceTelemetry.battery_percent)
            .where(
                DeviceTelemetry.device_id == device_id,
                DeviceTelemetry.recorded_at >= since,
                DeviceTelemetry.battery_percent.is_not(None),
            )
            .order_by(DeviceTelemetry.recorded_at)
            .limit(limit)
        )
        return [
            BatterySample(recorded_at=row.recorded_at, percent=int(row.battery_percent))
            for row in await self._session.execute(stmt)
        ]

    async def event_type_counts(
        self, device_id: uuid.UUID, *, since: datetime
    ) -> list[EventTypeCount]:
        """What the device has been reporting, most frequent first."""
        stmt = (
            select(DeviceTelemetry.event_type, func.count().label("event_count"))
            .where(self._scope(device_id, since, event_type=None))
            .group_by(DeviceTelemetry.event_type)
            .order_by(func.count().desc(), DeviceTelemetry.event_type)
        )
        return [
            EventTypeCount(event_type=row.event_type, count=int(row.event_count))
            for row in await self._session.execute(stmt)
        ]

    async def coverage(self, device_id: uuid.UUID, *, since: datetime, timezone: str) -> Coverage:
        """How much evidence exists, in one query."""
        day = func.date(self._local(timezone))
        stmt = select(
            func.count().label("total"),
            func.min(DeviceTelemetry.recorded_at).label("first"),
            func.max(DeviceTelemetry.recorded_at).label("last"),
            func.count(func.distinct(day)).label("days"),
        ).where(self._scope(device_id, since, event_type=None))

        row = (await self._session.execute(stmt)).one()
        return Coverage(
            total_events=int(row.total),
            first_event_at=row.first,
            last_event_at=row.last,
            distinct_days=int(row.days),
        )

    async def heartbeat_gaps(
        self, device_id: uuid.UUID, *, since: datetime, minimum_seconds: int, limit: int = 20
    ) -> list[Gap]:
        """Silences longer than ``minimum_seconds`` between heartbeats.

        Reported as observed gaps rather than as an uptime percentage. A
        percentage needs a denominator of time the device was *expected* to
        be reachable, and nothing here knows that -- a device switched off
        deliberately would be scored identically to one that crashed.
        """
        previous = func.lag(DeviceTelemetry.recorded_at).over(order_by=DeviceTelemetry.recorded_at)
        beats = (
            select(
                DeviceTelemetry.recorded_at.label("at"),
                previous.label("previous_at"),
            )
            .where(self._scope(device_id, since, event_type=HEARTBEAT_EVENT))
            .subquery()
        )

        seconds = func.extract("epoch", beats.c.at - beats.c.previous_at)
        stmt = (
            select(beats.c.previous_at, beats.c.at, seconds.label("seconds"))
            .where(beats.c.previous_at.is_not(None), seconds > minimum_seconds)
            .order_by(seconds.desc())
            .limit(limit)
        )

        return [
            Gap(
                started_at=row.previous_at,
                ended_at=row.at,
                seconds=int(row.seconds),
            )
            for row in await self._session.execute(stmt)
        ]
