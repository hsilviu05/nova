"""The statistics behind the insights, and the thresholds that gate them.

These matter more than they look. Every function here produces a sentence
that appears on a screen as a statement of fact about somebody's life, so the
tests are written around two questions: does it compute the right number, and
does it stay quiet when it should not be speaking at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nova.services.insights import (
    MIN_BATTERY_FIT_R2,
    MIN_BATTERY_SAMPLES,
    active_hours_insight,
    battery_insight,
    busiest_band,
    busiest_weekday_insight,
    connectivity_insight,
    current_discharge_segment,
    least_squares,
    percentage_change,
    support_confidence,
    weekly_trend_insight,
)

START = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _hours(**counts: int) -> list[int]:
    """24 hourly counts from keyword pairs like ``h9=40``."""
    hourly = [0] * 24
    for key, value in counts.items():
        hourly[int(key.removeprefix("h"))] = value
    return hourly


def _discharge(
    *, points: int, percent_per_hour: float, start: int = 100, minutes: int = 30
) -> list[tuple[datetime, int]]:
    """A clean linear discharge."""
    return [
        (
            START + timedelta(minutes=minutes * n),
            round(start - percent_per_hour * (minutes * n / 60)),
        )
        for n in range(points)
    ]


class TestLeastSquares:
    def test_recovers_a_known_line(self) -> None:
        # y = 3x + 2, exactly.
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        slope, intercept, r_squared = least_squares(xs, [3 * x + 2 for x in xs])

        assert slope == pytest.approx(3.0)
        assert intercept == pytest.approx(2.0)
        assert r_squared == pytest.approx(1.0)

    def test_recovers_a_negative_slope(self) -> None:
        xs = [0.0, 1.0, 2.0, 3.0]
        slope, _, _ = least_squares(xs, [100 - 7 * x for x in xs])
        assert slope == pytest.approx(-7.0)

    def test_noise_explains_little_variance(self) -> None:
        # A slope fitted through scatter is still a slope. R² is what tells
        # the caller not to trust it.
        xs = [float(n) for n in range(10)]
        ys = [50.0, 10.0, 90.0, 20.0, 70.0, 5.0, 95.0, 30.0, 60.0, 15.0]
        _, _, r_squared = least_squares(xs, ys)
        assert r_squared < 0.3

    def test_no_spread_in_x_is_not_a_fit(self) -> None:
        slope, intercept, r_squared = least_squares([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])
        assert slope == 0.0
        assert intercept == pytest.approx(2.0)
        assert r_squared == 0.0

    def test_a_flat_line_reports_no_explained_variance(self) -> None:
        """A constant y is a perfect fit that explains nothing.

        Returning 1.0 here would let a battery sitting at 80% pass the fit
        threshold and produce a runtime projection from a slope of zero.
        """
        _, _, r_squared = least_squares([0.0, 1.0, 2.0], [80.0, 80.0, 80.0])
        assert r_squared == 0.0

    def test_empty_input(self) -> None:
        assert least_squares([], []) == (0.0, 0.0, 0.0)


class TestBusiestBand:
    def test_finds_a_working_day(self) -> None:
        assert busiest_band(_hours(h9=30, h10=40, h11=35, h3=1, h22=2)) == (9, 11)

    def test_wraps_around_midnight(self) -> None:
        """An evening person must not be described as active all day.

        Any non-wrapping implementation returns something like (0, 23) here,
        which is true and useless.
        """
        assert busiest_band(_hours(h22=30, h23=40, h0=35, h1=20)) == (22, 0)

    def test_a_single_spike(self) -> None:
        assert busiest_band(_hours(h14=100)) == (14, 14)

    def test_no_activity(self) -> None:
        assert busiest_band([0] * 24) is None

    def test_flat_activity_needs_most_of_the_day(self) -> None:
        # Uniform activity genuinely has no band: the shortest run covering
        # 80% is 20 hours, and the caller rejects spans that wide.
        band = busiest_band([10] * 24)
        assert band is not None
        start, end = band
        assert (end - start) % 24 + 1 == 20

    def test_rejects_the_wrong_shape(self) -> None:
        with pytest.raises(ValueError, match="24 hourly counts"):
            busiest_band([1, 2, 3])


class TestPercentageChange:
    def test_increase(self) -> None:
        assert percentage_change(100, 150) == pytest.approx(0.5)

    def test_decrease(self) -> None:
        assert percentage_change(100, 60) == pytest.approx(-0.4)

    def test_from_zero_is_not_a_percentage(self) -> None:
        assert percentage_change(0, 40) is None


class TestActiveHours:
    def test_reports_a_band(self) -> None:
        insight = active_hours_insight(_hours(h9=40, h10=50, h11=45), days_observed=10)

        assert insight is not None
        assert insight.kind == "active_hours"
        assert "09:00" in insight.headline
        # Inclusive end hour 11 is rendered as 12:00, because "between 9 and
        # 11" would exclude an hour the person is actually there for.
        assert "12:00" in insight.headline
        assert insight.sample_size == 135
        assert insight.days_observed == 10

    def test_stays_quiet_on_too_few_days(self) -> None:
        """Hundreds of events from two days is two days of evidence."""
        assert active_hours_insight(_hours(h9=400), days_observed=2) is None

    def test_stays_quiet_on_too_few_events(self) -> None:
        assert active_hours_insight(_hours(h9=5), days_observed=30) is None

    def test_stays_quiet_when_the_band_is_the_whole_day(self) -> None:
        # Someone -- or something -- registering evenly around the clock has
        # no active hours, and saying "you're usually around 00:00 to 23:00"
        # is worse than saying nothing.
        assert active_hours_insight([20] * 24, days_observed=30) is None

    def test_no_activity_at_all(self) -> None:
        assert active_hours_insight([0] * 24, days_observed=30) is None


class TestBusiestWeekday:
    def test_compares_per_occurrence_not_per_total(self) -> None:
        """The core methodological point of this insight.

        Monday has 90 events over 3 Mondays (30 each); Saturday has 80 over 2
        Saturdays (40 each). Raw totals say Monday. Per occurrence -- which is
        the actual question -- says Saturday.
        """
        insight = busiest_weekday_insight(
            {0: 90, 5: 80},
            {0: 3, 5: 2},
            days_observed=17,
        )

        assert insight is not None
        assert "Saturday" in insight.headline
        assert "Monday" not in insight.headline

    def test_needs_two_weeks(self) -> None:
        assert busiest_weekday_insight({0: 90, 5: 80}, {0: 3, 5: 2}, days_observed=10) is None

    def test_ignores_weekdays_seen_once(self) -> None:
        # One Saturday is an anecdote. With Saturday excluded only Monday
        # remains, and one day is not a comparison.
        assert busiest_weekday_insight({0: 90, 5: 500}, {0: 3, 5: 1}, days_observed=20) is None

    def test_a_dead_heat_says_nothing(self) -> None:
        assert busiest_weekday_insight({0: 60, 5: 40}, {0: 3, 5: 2}, days_observed=20) is None


class TestWeeklyTrend:
    def test_reports_a_real_increase(self) -> None:
        insight = weekly_trend_insight(previous_week=40, current_week=60, days_observed=14)

        assert insight is not None
        assert "50%" in insight.headline
        assert "more" in insight.headline

    def test_reports_a_decrease(self) -> None:
        insight = weekly_trend_insight(previous_week=100, current_week=50, days_observed=14)
        assert insight is not None
        assert "less" in insight.headline

    def test_ignores_a_small_change(self) -> None:
        # 5% week to week is noise wearing a decimal point.
        assert weekly_trend_insight(previous_week=100, current_week=105, days_observed=14) is None

    def test_ignores_thin_weeks(self) -> None:
        assert weekly_trend_insight(previous_week=2, current_week=8, days_observed=14) is None


class TestDischargeSegment:
    def test_a_clean_discharge_is_one_segment(self) -> None:
        samples = _discharge(points=10, percent_per_hour=5)
        assert len(current_discharge_segment(samples)) == 10

    def test_a_recharge_starts_a_new_segment(self) -> None:
        """Everything before the plug is a different curve.

        Fitting across a recharge averages a rise and a fall into a slope
        that describes neither.
        """
        samples = [
            (START, 40),
            (START + timedelta(minutes=30), 35),
            (START + timedelta(minutes=60), 95),  # plugged in
            (START + timedelta(minutes=90), 90),
            (START + timedelta(minutes=120), 85),
        ]
        segment = current_discharge_segment(samples)

        assert [level for _, level in segment] == [95, 90, 85]

    def test_gauge_noise_does_not_split(self) -> None:
        # A 1-point wobble is the ADC, not a charger. Splitting on it would
        # leave every segment too short to fit.
        samples = [
            (START, 60),
            (START + timedelta(minutes=30), 61),
            (START + timedelta(hours=1), 58),
        ]
        assert len(current_discharge_segment(samples)) == 3

    def test_a_gradual_charge_starts_a_new_segment(self) -> None:
        """A charge sampled densely never jumps, and must still be detected.

        This is what a real device produces. The firmware heartbeats every 30
        seconds, so a 15%/hour charge climbs 0.125% per sample -- comparing
        each reading against the one before it sees a rise of at most one
        point and concludes the battery is not charging.

        The consequence was not a wrong number. The fit ran across the rise
        and the fall together, R² collapsed, the gate refused to publish, and
        the battery projection silently stopped existing on any device that
        had ever been plugged in.
        """
        samples: list[tuple[datetime, int]] = []
        # 40% -> 100% over four hours, sampled every 30 seconds.
        for n in range(481):
            samples.append((START + timedelta(seconds=30 * n), round(40 + 60 * n / 480)))
        # Then eight hours of discharge at 6%/hour.
        charged_at = START + timedelta(hours=4)
        for n in range(1, 961):
            samples.append((charged_at + timedelta(seconds=30 * n), round(100 - 6 * n / 120)))

        segment = current_discharge_segment(samples)

        # The segment must be the discharge, not the whole series.
        assert len(segment) < len(samples)
        assert segment[0][1] >= 97, "should start at or near the top of the charge"
        assert segment[-1] == samples[-1]
        assert segment[0][1] > segment[-1][1]

    def test_a_slow_charge_is_not_mistaken_for_noise(self) -> None:
        # Half-hourly readings climbing two points at a time: within the
        # neighbour-to-neighbour tolerance at every step, but unmistakably a
        # charge across the series.
        samples = [(START + timedelta(minutes=30 * n), 50 + 2 * n) for n in range(8)]
        assert len(current_discharge_segment(samples)) < len(samples)

    def test_empty(self) -> None:
        assert current_discharge_segment([]) == []


class TestBatteryInsight:
    def test_projects_from_a_clean_discharge(self) -> None:
        # 10%/hour from 100%, sampled half-hourly. At the last reading the
        # level is 55%, so the projection should be about 5h30m.
        insight = battery_insight(_discharge(points=10, percent_per_hour=10), days_observed=5)

        assert insight is not None
        assert insight.kind == "battery_runtime"
        assert "5h 30m" in insight.headline
        assert "10.0% an hour" in insight.detail

    def test_refuses_too_few_readings(self) -> None:
        samples = _discharge(points=MIN_BATTERY_SAMPLES - 1, percent_per_hour=10)
        assert battery_insight(samples, days_observed=5) is None

    def test_refuses_too_short_a_span(self) -> None:
        # Ten readings a minute apart is ten minutes of evidence, however
        # neatly they line up.
        samples = _discharge(points=10, percent_per_hour=10, minutes=1)
        assert battery_insight(samples, days_observed=5) is None

    def test_refuses_a_rising_battery(self) -> None:
        charging = [(START + timedelta(minutes=30 * n), 40 + 5 * n) for n in range(10)]
        assert battery_insight(charging, days_observed=5) is None

    def test_refuses_a_poor_fit(self) -> None:
        """A slope through a curve is still a slope; R² is what stops it.

        The readings have to be monotonically falling, or segment-splitting
        rejects them first and this would pass without exercising the fit
        check at all. So: a fast drop that then plateaus. One discharge, no
        recharge, a clearly negative slope -- and a shape a straight line
        does not describe (R² ≈ 0.68).

        Without the fit check this returns a confident runtime figure
        extrapolated from a trend that is not there.
        """
        plateauing = [
            (START + timedelta(minutes=30 * n), level)
            for n, level in enumerate([100, 70, 45, 30, 25, 24, 23, 22, 21, 20])
        ]
        # The guard being tested is the fit, not the segmenting: prove the
        # readings survive as one segment before asserting on the outcome.
        assert len(current_discharge_segment(plateauing)) == len(plateauing)
        assert battery_insight(plateauing, days_observed=5) is None

    def test_a_flat_battery_reading_is_not_a_projection(self) -> None:
        flat = [(START + timedelta(minutes=30 * n), 80) for n in range(10)]
        assert battery_insight(flat, days_observed=5) is None

    def test_reports_the_fit_quality(self) -> None:
        insight = battery_insight(_discharge(points=12, percent_per_hour=8), days_observed=5)
        assert insight is not None
        assert "R²=" in insight.detail
        # A near-perfect fit earns the higher confidence label.
        assert insight.confidence == "high"

    def test_the_threshold_is_where_it_says_it_is(self) -> None:
        assert 0.0 < MIN_BATTERY_FIT_R2 < 1.0


class TestConnectivity:
    def test_reports_observed_gaps(self) -> None:
        insight = connectivity_insight(longest_gap_seconds=7200, gap_count=4, days_observed=10)

        assert insight is not None
        assert "4 times" in insight.headline
        assert "2 hours" in insight.detail
        # The caveat is part of the statement, not a footnote: nothing here
        # can tell a crash from someone pulling the plug.
        assert "deliberate power-off" in insight.detail

    def test_singular_reads_properly(self) -> None:
        insight = connectivity_insight(longest_gap_seconds=600, gap_count=1, days_observed=10)
        assert insight is not None
        assert "1 time" in insight.headline
        assert "1 times" not in insight.headline

    def test_no_gaps_is_not_an_insight(self) -> None:
        assert connectivity_insight(longest_gap_seconds=None, gap_count=0, days_observed=10) is None


class TestSupportConfidence:
    @pytest.mark.parametrize(
        ("days", "samples", "expected"),
        [
            (30, 500, "high"),
            (14, 200, "high"),
            (10, 100, "medium"),
            (7, 60, "medium"),
            (3, 25, "low"),
            # Plenty of events, barely any days: still low, because days are
            # what a habit claim rests on.
            (3, 5000, "low"),
        ],
    )
    def test_grades(self, days: int, samples: int, expected: str) -> None:
        assert support_confidence(sample_size=samples, days_observed=days) == expected
