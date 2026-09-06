"""Analytics end to end, against real Postgres.

The load-bearing test in this file is the daylight-saving one. Bucketing
telemetry into local hours is the whole basis of every "you're usually around
at..." statement, and getting it wrong produces numbers that look completely
reasonable and are off by an hour for half the year.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from nova.core.clock import utc_now
from nova.models.device import Device, DeviceTelemetry
from nova.models.user import User
from nova.repositories.analytics import AnalyticsRepository
from nova.services.analytics import AnalyticsService

pytestmark = pytest.mark.integration

BUCHAREST = "Europe/Bucharest"


async def _device(session: AsyncSession, *, timezone: str = "UTC") -> Device:
    user = User(
        email=f"analytics-{uuid.uuid4().hex[:10]}@example.com",
        password_hash="not-a-real-hash",
        display_name="Analytics",
        timezone=timezone,
    )
    session.add(user)
    await session.flush()

    device = Device(hardware_id=f"hw-{uuid.uuid4().hex[:12]}", user_id=user.id, model="nova-1")
    session.add(device)
    await session.flush()
    return device


def _event(
    device: Device,
    at: datetime,
    *,
    event_type: str = "person_detected",
    battery: int | None = None,
) -> DeviceTelemetry:
    return DeviceTelemetry(
        device_id=device.id,
        event_type=event_type,
        recorded_at=at,
        battery_percent=battery,
        payload={},
    )


async def _seed(session: AsyncSession, device: Device, events: list[DeviceTelemetry]) -> None:
    session.add_all(events)
    await session.flush()


class TestLocalTimeBucketing:
    async def test_daylight_saving_is_resolved_by_the_database(self, session: AsyncSession) -> None:
        """The reason bucketing happens in Postgres and not in Python.

        Europe/Bucharest springs forward on 2026-03-29: 03:00 local never
        happens. 00:30 UTC is 02:30 local, and 01:30 UTC is 04:30 local --
        an hour apart in UTC, two hours apart locally. Any implementation
        that adds a fixed offset puts the second event at 03:30, an hour that
        did not exist.
        """
        device = await _device(session, timezone=BUCHAREST)
        await _seed(
            session,
            device,
            [
                _event(device, datetime(2026, 3, 29, 0, 30, tzinfo=UTC)),
                _event(device, datetime(2026, 3, 29, 1, 30, tzinfo=UTC)),
            ],
        )

        repository = AnalyticsRepository(session)
        since = datetime(2026, 1, 1, tzinfo=UTC)

        local = await repository.by_hour(device.id, since=since, timezone=BUCHAREST)
        assert [b.hour for b in local if b.count] == [2, 4]

        # The same rows in UTC, to show the difference is real and not an
        # artefact of the fixture.
        utc = await repository.by_hour(device.id, since=since, timezone="UTC")
        assert [b.hour for b in utc if b.count] == [0, 1]

    async def test_autumn_repeats_an_hour(self, session: AsyncSession) -> None:
        """The other side of it: 2026-10-25 has 03:00 local twice.

        Both events land in the same local hour despite being an hour apart
        in UTC. Counting them separately would be wrong in the opposite
        direction from the spring case.
        """
        device = await _device(session, timezone=BUCHAREST)
        await _seed(
            session,
            device,
            [
                _event(device, datetime(2026, 10, 24, 23, 30, tzinfo=UTC)),
                _event(device, datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
            ],
        )

        buckets = await AnalyticsRepository(session).by_hour(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), timezone=BUCHAREST
        )
        assert [(b.hour, b.count) for b in buckets if b.count] == [(2, 1), (3, 1)]

    async def test_a_local_day_is_not_a_utc_day(self, session: AsyncSession) -> None:
        """23:30 UTC is already tomorrow in Bucharest."""
        device = await _device(session, timezone=BUCHAREST)
        await _seed(
            session,
            device,
            [_event(device, datetime(2026, 7, 14, 23, 30, tzinfo=UTC))],
        )

        repository = AnalyticsRepository(session)
        since = datetime(2026, 1, 1, tzinfo=UTC)

        local = await repository.by_day(device.id, since=since, timezone=BUCHAREST)
        assert [b.day for b in local] == [date(2026, 7, 15)]

        utc = await repository.by_day(device.id, since=since, timezone="UTC")
        assert [b.day for b in utc] == [date(2026, 7, 14)]

    async def test_weekday_uses_the_python_convention(self, session: AsyncSession) -> None:
        """0 is Monday on both sides of the boundary.

        Postgres counts Sunday as 0. Returning that unconverted would put
        every insight one day out, and the label would still read plausibly.
        """
        device = await _device(session)
        # 2026-09-07 is a Monday; assert that rather than trusting the date.
        monday = date(2026, 9, 7)
        assert monday.weekday() == 0

        await _seed(
            session,
            device,
            [_event(device, datetime(2026, 9, 7, 10, 0, tzinfo=UTC))],
        )

        grid = await AnalyticsRepository(session).by_weekday_hour(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), timezone="UTC"
        )
        assert [(c.weekday, c.hour, c.count) for c in grid] == [(0, 10, 1)]

    async def test_a_sunday_is_six(self, session: AsyncSession) -> None:
        device = await _device(session)
        sunday = date(2026, 9, 6)
        assert sunday.weekday() == 6

        await _seed(session, device, [_event(device, datetime(2026, 9, 6, 10, 0, tzinfo=UTC))])

        grid = await AnalyticsRepository(session).by_weekday_hour(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), timezone="UTC"
        )
        assert grid[0].weekday == 6


class TestAggregates:
    async def test_every_hour_is_present_even_when_empty(self, session: AsyncSession) -> None:
        """A chart that omits quiet hours reads as a busy night."""
        device = await _device(session)
        await _seed(session, device, [_event(device, datetime(2026, 9, 1, 9, 0, tzinfo=UTC))])

        buckets = await AnalyticsRepository(session).by_hour(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), timezone="UTC"
        )
        assert len(buckets) == 24
        assert [b.hour for b in buckets] == list(range(24))

    async def test_presence_counts_exclude_heartbeats(self, session: AsyncSession) -> None:
        """Heartbeats arrive whether anyone is there or not.

        Counting them as presence would measure that the device is plugged
        in, and then describe that as somebody's daily routine.
        """
        device = await _device(session)
        at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        await _seed(
            session,
            device,
            [
                _event(device, at),
                *[
                    _event(device, at + timedelta(seconds=30 * n), event_type="heartbeat")
                    for n in range(50)
                ],
            ],
        )

        repository = AnalyticsRepository(session)
        since = datetime(2026, 1, 1, tzinfo=UTC)

        presence = await repository.by_hour(device.id, since=since, timezone="UTC")
        assert sum(b.count for b in presence) == 1

        # The counts are still available, just not as presence.
        types = {
            t.event_type: t.count
            for t in await repository.event_type_counts(device.id, since=since)
        }
        assert types == {"heartbeat": 50, "person_detected": 1}

    async def test_the_window_excludes_older_events(self, session: AsyncSession) -> None:
        device = await _device(session)
        now = utc_now()
        await _seed(
            session,
            device,
            [
                _event(device, now - timedelta(days=2)),
                _event(device, now - timedelta(days=40)),
            ],
        )

        repository = AnalyticsRepository(session)
        recent = await repository.coverage(
            device.id, since=now - timedelta(days=30), timezone="UTC"
        )
        assert recent.total_events == 1

        everything = await repository.coverage(
            device.id, since=now - timedelta(days=90), timezone="UTC"
        )
        assert everything.total_events == 2

    async def test_coverage_counts_days_not_events(self, session: AsyncSession) -> None:
        """The denominator that stops one busy afternoon becoming a habit."""
        device = await _device(session)
        at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        await _seed(
            session,
            device,
            [_event(device, at + timedelta(minutes=n)) for n in range(200)],
        )

        coverage = await AnalyticsRepository(session).coverage(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), timezone="UTC"
        )
        assert coverage.total_events == 200
        assert coverage.distinct_days == 1

    async def test_battery_series_is_oldest_first(self, session: AsyncSession) -> None:
        """A reversed series fits a slope of the right size and wrong sign."""
        device = await _device(session)
        start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        await _seed(
            session,
            device,
            [
                _event(
                    device,
                    start + timedelta(minutes=30 * n),
                    event_type="heartbeat",
                    battery=100 - 5 * n,
                )
                for n in range(6)
            ],
        )

        series = await AnalyticsRepository(session).battery_series(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert [s.percent for s in series] == [100, 95, 90, 85, 80, 75]

    async def test_rows_without_a_battery_reading_are_skipped(self, session: AsyncSession) -> None:
        device = await _device(session)
        at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        await _seed(
            session,
            device,
            [
                _event(device, at, battery=80),
                _event(device, at + timedelta(minutes=1)),
            ],
        )

        series = await AnalyticsRepository(session).battery_series(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert [s.percent for s in series] == [80]


class TestGaps:
    async def test_finds_a_silence_between_heartbeats(self, session: AsyncSession) -> None:
        device = await _device(session)
        start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        beats = [start, start + timedelta(seconds=30), start + timedelta(hours=3)]
        await _seed(
            session,
            device,
            [_event(device, at, event_type="heartbeat") for at in beats],
        )

        gaps = await AnalyticsRepository(session).heartbeat_gaps(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), minimum_seconds=120
        )

        assert len(gaps) == 1
        assert gaps[0].seconds == pytest.approx(3 * 3600 - 30, abs=1)

    async def test_regular_heartbeats_produce_no_gaps(self, session: AsyncSession) -> None:
        device = await _device(session)
        start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        await _seed(
            session,
            device,
            [
                _event(device, start + timedelta(seconds=30 * n), event_type="heartbeat")
                for n in range(20)
            ],
        )

        gaps = await AnalyticsRepository(session).heartbeat_gaps(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), minimum_seconds=120
        )
        assert gaps == []

    async def test_gaps_are_longest_first(self, session: AsyncSession) -> None:
        device = await _device(session)
        start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        beats = [
            start,
            start + timedelta(minutes=10),
            start + timedelta(minutes=70),
            start + timedelta(minutes=80),
        ]
        await _seed(
            session,
            device,
            [_event(device, at, event_type="heartbeat") for at in beats],
        )

        gaps = await AnalyticsRepository(session).heartbeat_gaps(
            device.id, since=datetime(2026, 1, 1, tzinfo=UTC), minimum_seconds=300
        )
        assert [g.seconds for g in gaps] == [3600, 600, 600]


class TestOwnershipIsolation:
    async def test_one_devices_telemetry_never_reaches_another(self, session: AsyncSession) -> None:
        """Every aggregate is scoped by device id in the WHERE clause."""
        mine = await _device(session)
        theirs = await _device(session)
        at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        await _seed(
            session,
            theirs,
            [_event(theirs, at + timedelta(minutes=n)) for n in range(30)],
        )

        repository = AnalyticsRepository(session)
        since = datetime(2026, 1, 1, tzinfo=UTC)

        assert (await repository.coverage(mine.id, since=since, timezone="UTC")).total_events == 0
        assert (
            sum(b.count for b in await repository.by_hour(mine.id, since=since, timezone="UTC"))
            == 0
        )
        assert await repository.by_day(mine.id, since=since, timezone="UTC") == []
        assert await repository.event_type_counts(mine.id, since=since) == []


class TestInsightsService:
    def _service(self, session: AsyncSession) -> AnalyticsService:
        return AnalyticsService(
            analytics=AnalyticsRepository(session), heartbeat_interval_seconds=30
        )

    async def test_no_telemetry_says_so_plainly(self, session: AsyncSession) -> None:
        device = await _device(session)

        result = await self._service(session).insights(device.id, window_days=30, timezone="UTC")

        assert result.insights == []
        assert result.insufficient_reason == "NOVA hasn't reported anything yet."
        assert result.coverage.is_sufficient is False

    async def test_one_busy_day_is_not_enough(self, session: AsyncSession) -> None:
        """Volume is not evidence. This is the failure the phase exists for.

        Two hundred detections in a single afternoon would let a naive
        implementation announce a daily routine after one day.
        """
        device = await _device(session)
        at = utc_now() - timedelta(days=1)
        await _seed(
            session,
            device,
            [_event(device, at + timedelta(seconds=30 * n)) for n in range(200)],
        )

        result = await self._service(session).insights(device.id, window_days=30, timezone="UTC")

        assert result.insights == []
        assert result.insufficient_reason is not None
        assert "day of data" in result.insufficient_reason

    async def test_a_consistent_routine_is_reported(self, session: AsyncSession) -> None:
        """Three weeks of mornings, which is a habit by any reading."""
        device = await _device(session)
        now = utc_now()
        events = []
        for day in range(21):
            midnight = (now - timedelta(days=day)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for hour in (9, 10, 11):
                for minute in (0, 20, 40):
                    events.append(_event(device, midnight + timedelta(hours=hour, minutes=minute)))
        await _seed(session, device, events)

        result = await self._service(session).insights(device.id, window_days=30, timezone="UTC")

        kinds = {i.kind for i in result.insights}
        assert "active_hours" in kinds
        assert result.insufficient_reason is None

        active = next(i for i in result.insights if i.kind == "active_hours")
        assert "09:00" in active.headline
        assert active.days_observed >= 20
        # The evidence travels with the claim.
        assert active.sample_size == len(events)
        # 189 events over 21 days: plenty of days, just under the 200-event
        # bar for "high". Grading on both is the point -- days alone would
        # let a thin but long-running device sound authoritative.
        assert active.confidence == "medium"

    async def test_sufficient_data_can_still_yield_nothing(self, session: AsyncSession) -> None:
        """Enough to look at, nothing consistent enough to say.

        Distinct from having no data, and the app shows it differently.
        """
        device = await _device(session)
        now = utc_now()
        # Scattered across the clock over three days: real telemetry, no
        # pattern in it.
        events = [
            _event(device, now - timedelta(days=day, hours=hour))
            for day in range(3)
            for hour in range(0, 24, 3)
        ]
        await _seed(session, device, events)

        result = await self._service(session).insights(device.id, window_days=30, timezone="UTC")

        assert result.coverage.is_sufficient is True
        assert result.insights == []
        assert result.insufficient_reason is not None
        assert "still watching" in result.insufficient_reason

    async def test_the_overview_echoes_the_timezone_it_used(self, session: AsyncSession) -> None:
        """A client rendering "14:00" needs to know whose two o'clock."""
        device = await _device(session, timezone=BUCHAREST)

        overview = await self._service(session).overview(
            device.id, window_days=30, timezone=BUCHAREST
        )
        assert overview.timezone == BUCHAREST
        assert overview.window_days == 30
        assert len(overview.presence_by_hour) == 24

    async def test_an_unusable_timezone_falls_back_rather_than_failing(
        self, session: AsyncSession
    ) -> None:
        """A name the database cannot resolve must not 500 the screen.

        The schema layer rejects unknown names on write; this is what
        happens if one reaches the read path anyway.
        """
        device = await _device(session)

        overview = await self._service(session).overview(
            device.id, window_days=30, timezone="Mars/Olympus_Mons"
        )
        assert overview.timezone == "UTC"


class TestAnalyticsRoutes:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/devices/{uuid.uuid4()}/analytics")
        assert response.status_code == 401

    async def test_someone_elses_device_is_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str], session: AsyncSession
    ) -> None:
        """Not forbidden: existence is not confirmed to a stranger."""
        theirs = await _device(session)

        response = await client.get(f"/api/v1/devices/{theirs.id}/analytics", headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "device_not_found"

    async def test_rejects_a_window_beyond_the_cap(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Each request is a scan, so the window is bounded."""
        response = await client.get(
            f"/api/v1/devices/{uuid.uuid4()}/analytics",
            headers=auth_headers,
            params={"window_days": 3650},
        )
        assert response.status_code == 422

    async def test_returns_the_overview_for_an_owned_device(
        self, client: AsyncClient, auth_headers: dict[str, str], session: AsyncSession
    ) -> None:
        device_id = await _claim_a_device(client, auth_headers)
        assert device_id is not None

        response = await client.get(
            f"/api/v1/devices/{device_id}/analytics",
            headers=auth_headers,
            params={"window_days": 7},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["window_days"] == 7
        assert body["timezone"] == "UTC"
        assert len(body["presence_by_hour"]) == 24
        assert body["coverage"]["is_sufficient"] is False

    async def test_insights_for_a_silent_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device_id = await _claim_a_device(client, auth_headers)

        response = await client.get(f"/api/v1/devices/{device_id}/insights", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        # An empty list with a reason is a normal response, not an error.
        assert body["insights"] == []
        assert body["insufficient_reason"]


class TestTimezoneProfile:
    async def test_defaults_to_utc(self, client: AsyncClient, auth_headers: dict[str, str]) -> None:
        body = (await client.get("/api/v1/users/me", headers=auth_headers)).json()
        assert body["timezone"] == "UTC"

    async def test_can_be_changed(self, client: AsyncClient, auth_headers: dict[str, str]) -> None:
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"timezone": BUCHAREST}
        )
        assert response.status_code == 200
        assert response.json()["timezone"] == BUCHAREST

    async def test_rejects_a_name_the_database_cannot_resolve(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Rejected on write, not discovered at query time.

        An unknown name reaches an AT TIME ZONE clause, so accepting one here
        would turn every later analytics request for this account into a 500.
        """
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"timezone": "Middle/Earth"}
        )
        assert response.status_code == 422

    async def test_rejects_a_fixed_offset(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        # "+02:00" is wrong for half the year everywhere that observes DST.
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"timezone": "+02:00"}
        )
        assert response.status_code == 422

    async def test_analytics_uses_the_profile_timezone(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        await client.patch("/api/v1/users/me", headers=auth_headers, json={"timezone": BUCHAREST})
        device_id = await _claim_a_device(client, auth_headers)

        body = (
            await client.get(f"/api/v1/devices/{device_id}/analytics", headers=auth_headers)
        ).json()
        assert body["timezone"] == BUCHAREST


async def _claim_a_device(client: AsyncClient, headers: dict[str, str]) -> str:
    """Provision and claim a device through the real flow."""
    provisioned = (
        await client.post(
            "/api/v1/devices/provision",
            json={"hardware_id": f"hw-{uuid.uuid4().hex[:12]}", "model": "nova-1"},
        )
    ).json()

    claimed = await client.post(
        "/api/v1/devices/claim",
        headers=headers,
        json={"code": provisioned["claim_code"]},
    )
    assert claimed.status_code in (200, 201), claimed.text
    device_id: Any = claimed.json()["id"]
    return str(device_id)
