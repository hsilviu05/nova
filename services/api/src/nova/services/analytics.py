"""Analytics: aggregates for charts, and insights derived from them.

The service owns the decision that :mod:`nova.services.insights` cannot make
on its own -- what window to look at, whose timezone to use, and whether
there is enough data to say anything at all. The statistics stay pure and
testable; the orchestration lives here.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nova.core.clock import utc_now
from nova.core.logging import get_logger
from nova.core.timezones import normalise
from nova.repositories.analytics import (
    HEARTBEAT_EVENT,
    AnalyticsRepository,
    Coverage,
    DayCount,
)
from nova.schemas.analytics import (
    AnalyticsRead,
    BatteryPoint,
    CoverageRead,
    DayBucket,
    EventTypeBucket,
    GapRead,
    HourBucket,
    InsightRead,
    InsightsRead,
    WeekdayHourBucket,
)
from nova.services import insights as stats

logger = get_logger(__name__)

# A heartbeat is expected every ``heartbeat_interval_seconds``; a silence of
# several intervals is a real absence rather than one late frame.
GAP_INTERVALS = 4

# Below this there is nothing worth charting, let alone interpreting.
MIN_EVENTS_FOR_ANY_INSIGHT = 10
MIN_DAYS_FOR_ANY_INSIGHT = 2

# Enough data to look at, but every individual test fell short of its own
# threshold. Distinct from having no data, and the app says so differently.
NOTHING_CONSISTENT_YET = (
    "NOVA is still watching. Nothing it has seen is consistent enough to call a pattern yet."
)


class AnalyticsService:
    """Request-scoped analytics for one device."""

    def __init__(
        self,
        *,
        analytics: AnalyticsRepository,
        heartbeat_interval_seconds: int,
    ) -> None:
        self._analytics = analytics
        self._heartbeat_interval = heartbeat_interval_seconds

    async def overview(
        self, device_id: uuid.UUID, *, window_days: int, timezone: str
    ) -> AnalyticsRead:
        """Everything the insights screen plots, in one round trip.

        One response rather than six endpoints: the screen needs all of it to
        render anything, and six requests would be six round trips and six
        chances for the panels to disagree about the window.
        """
        zone = normalise(timezone)
        since = self._since(window_days)

        coverage = await self._analytics.coverage(device_id, since=since, timezone=zone)
        by_hour = await self._analytics.by_hour(device_id, since=since, timezone=zone)
        by_day = await self._analytics.by_day(device_id, since=since, timezone=zone)
        grid = await self._analytics.by_weekday_hour(device_id, since=since, timezone=zone)
        battery = await self._analytics.battery_series(device_id, since=since)
        types = await self._analytics.event_type_counts(device_id, since=since)
        gaps = await self._analytics.heartbeat_gaps(
            device_id,
            since=since,
            minimum_seconds=self._heartbeat_interval * GAP_INTERVALS,
        )

        return AnalyticsRead(
            window_days=window_days,
            timezone=zone,
            coverage=self._coverage_read(coverage),
            presence_by_hour=[HourBucket(hour=b.hour, count=b.count) for b in by_hour],
            presence_by_day=[DayBucket(day=b.day, count=b.count) for b in by_day],
            presence_by_weekday_hour=[
                WeekdayHourBucket(weekday=c.weekday, hour=c.hour, count=c.count) for c in grid
            ],
            battery=[BatteryPoint(recorded_at=s.recorded_at, percent=s.percent) for s in battery],
            event_types=[EventTypeBucket(event_type=t.event_type, count=t.count) for t in types],
            gaps=[
                GapRead(started_at=g.started_at, ended_at=g.ended_at, seconds=g.seconds)
                for g in gaps
            ],
        )

    async def insights(
        self, device_id: uuid.UUID, *, window_days: int, timezone: str
    ) -> InsightsRead:
        """What the data supports saying, and nothing more."""
        zone = normalise(timezone)
        since = self._since(window_days)

        coverage = await self._analytics.coverage(device_id, since=since, timezone=zone)
        coverage_read = self._coverage_read(coverage)

        if not coverage_read.is_sufficient:
            return InsightsRead(
                window_days=window_days,
                timezone=zone,
                coverage=coverage_read,
                insights=[],
                insufficient_reason=self._insufficient_reason(coverage),
            )

        by_hour = await self._analytics.by_hour(device_id, since=since, timezone=zone)
        by_day = await self._analytics.by_day(device_id, since=since, timezone=zone)
        battery = await self._analytics.battery_series(device_id, since=since)
        gaps = await self._analytics.heartbeat_gaps(
            device_id,
            since=since,
            minimum_seconds=self._heartbeat_interval * GAP_INTERVALS,
        )

        days = coverage.distinct_days
        totals, occurrences = self._weekday_tallies(by_day, zone, since)
        previous_week, current_week = self._week_totals(by_day, zone)

        found = [
            stats.active_hours_insight([b.count for b in by_hour], days_observed=days),
            stats.busiest_weekday_insight(totals, occurrences, days_observed=days),
            stats.weekly_trend_insight(
                previous_week=previous_week,
                current_week=current_week,
                days_observed=days,
            ),
            stats.battery_insight(
                [(s.recorded_at, s.percent) for s in battery], days_observed=days
            ),
            stats.connectivity_insight(
                longest_gap_seconds=gaps[0].seconds if gaps else None,
                gap_count=len(gaps),
                days_observed=days,
            ),
        ]
        # asdict, not vars: Insight uses __slots__ and has no __dict__.
        produced = [InsightRead(**dataclasses.asdict(i)) for i in found if i is not None]

        logger.info(
            "insights_computed",
            device_id=str(device_id),
            window_days=window_days,
            produced=len(produced),
            considered=len(found),
        )
        return InsightsRead(
            window_days=window_days,
            timezone=zone,
            coverage=coverage_read,
            insights=produced,
            insufficient_reason=None if produced else NOTHING_CONSISTENT_YET,
        )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _since(window_days: int) -> datetime:
        return utc_now() - timedelta(days=window_days)

    @staticmethod
    def _coverage_read(coverage: Coverage) -> CoverageRead:
        return CoverageRead(
            total_events=coverage.total_events,
            distinct_days=coverage.distinct_days,
            first_event_at=coverage.first_event_at,
            last_event_at=coverage.last_event_at,
            is_sufficient=(
                coverage.total_events >= MIN_EVENTS_FOR_ANY_INSIGHT
                and coverage.distinct_days >= MIN_DAYS_FOR_ANY_INSIGHT
            ),
        )

    @staticmethod
    def _insufficient_reason(coverage: Coverage) -> str:
        if coverage.total_events == 0:
            return "NOVA hasn't reported anything yet."
        if coverage.distinct_days < MIN_DAYS_FOR_ANY_INSIGHT:
            return (
                f"Only {coverage.distinct_days} day of data so far. "
                "Patterns need a few days to show up."
            )
        return (
            f"Only {coverage.total_events} events so far. "
            "There isn't enough here to draw anything from."
        )

    @staticmethod
    def _weekday_tallies(
        by_day: list[DayCount], timezone: str, since: datetime
    ) -> tuple[dict[int, int], dict[int, int]]:
        """Totals per weekday, and how many times each weekday came round.

        Occurrences are counted over the days the device was actually
        reporting -- from its first event in the window to its last -- rather
        than over the whole window. Counting a fortnight of days before the
        device was plugged in would halve every average for no reason.

        A reporting day with no events still counts as an occurrence. That is
        the point: a quiet Sunday is evidence about Sundays.
        """
        if not by_day:
            return {}, {}

        zone = ZoneInfo(timezone)
        first = min(day.day for day in by_day)
        last = max(day.day for day in by_day)
        # Never count days before the window opened, however early the first
        # event looks in local time.
        window_start = since.astimezone(zone).date()
        first = max(first, window_start)

        totals: dict[int, int] = {}
        for bucket in by_day:
            if bucket.day < first:  # pragma: no cover - clipped above
                continue
            totals[bucket.day.weekday()] = totals.get(bucket.day.weekday(), 0) + bucket.count

        occurrences: dict[int, int] = {}
        for offset in range((last - first).days + 1):
            weekday = (first + timedelta(days=offset)).weekday()
            occurrences[weekday] = occurrences.get(weekday, 0) + 1

        return totals, occurrences

    @staticmethod
    def _week_totals(by_day: list[DayCount], timezone: str) -> tuple[int, int]:
        """Events in the last seven local days, and the seven before that."""
        today = utc_now().astimezone(ZoneInfo(timezone)).date()
        current_from = today - timedelta(days=6)
        previous_from = today - timedelta(days=13)

        def total(start: date, end: date) -> int:
            return sum(b.count for b in by_day if start <= b.day <= end)

        return (
            total(previous_from, current_from - timedelta(days=1)),
            total(current_from, today),
        )


__all__ = ["HEARTBEAT_EVENT", "AnalyticsService"]
