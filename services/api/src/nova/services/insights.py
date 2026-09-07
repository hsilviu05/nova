"""Turning aggregates into statements, and refusing to when the data is thin.

Every function here is pure and takes plain values, so the statistics can be
tested without a database and the thresholds can be read in one place.

The governing rule: **an insight is only produced when the evidence supports
it.** A desk companion that says "you're usually here at nine" after watching
someone for one morning is not observant, it is guessing, and the guess is
indistinguishable from the real thing once it is on screen. So every
statement below carries the sample size and the number of distinct days it
came from, and every one has a documented minimum it will not go below --
returning ``None`` instead. "Not enough data yet" is a correct answer and the
API says it plainly.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# -- support thresholds -----------------------------------------------------
#
# Chosen for what the statement claims, not tuned to make demos work.

# "Usually" is a claim about a habit, and a habit needs to have been seen on
# several separate days. Events alone are not enough: four hundred detections
# from one afternoon is one day of evidence.
MIN_DAYS_FOR_HABIT = 3
MIN_EVENTS_FOR_HABIT = 20

# Comparing weekdays needs each weekday to have come round more than once,
# so the window has to span at least two full weeks.
MIN_DAYS_FOR_WEEKDAY = 14
MIN_OCCURRENCES_PER_WEEKDAY = 2

# A week-over-week change needs both weeks to be substantial, or the
# percentage is noise with a decimal point.
MIN_EVENTS_PER_WEEK = 15
# Below this, a change is not worth mentioning even when it is real.
MIN_MEANINGFUL_CHANGE = 0.20

# Battery projection.
MIN_BATTERY_SAMPLES = 8
MIN_BATTERY_SPAN_MINUTES = 45
# A discharge curve is not a straight line, but over a few hours it is close
# enough to extrapolate from. Below this the fit is not describing a trend.
MIN_BATTERY_FIT_R2 = 0.80
# A rise larger than this means the thing was plugged in; smaller rises are
# the gauge wobbling.
CHARGE_TOLERANCE_PERCENT = 2

# The share of presence events an "active hours" band has to cover.
#
# 0.8 rather than a bare majority. At 0.6 the band stops as soon as it has
# scraped past half, which drops hours just as busy as the ones it kept --
# and, worse, lets uniform round-the-clock activity produce a 15-hour "band"
# that slips under the whole-day guard below. Requiring most of the activity
# makes the claim both stronger and harder to satisfy by accident.
ACTIVE_BAND_COVERAGE = 0.80

Confidence = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class Insight:
    """One thing the data supports saying.

    ``sample_size`` and ``days_observed`` are part of the payload rather than
    a debugging aid: they are what lets somebody decide whether to believe
    the headline, and they are shown in the app for that reason.
    """

    kind: str
    headline: str
    detail: str
    confidence: Confidence
    sample_size: int
    days_observed: int


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def least_squares(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Fit ``y = slope * x + intercept``; return it with R².

    Written out rather than pulled from numpy: it is six lines, this is the
    only place in the backend that needs it, and a dependency carried into
    the container for six lines is not a good trade.

    R² is returned because it is the thing that decides whether the fit may
    be used at all. A slope from a scatter of noise looks exactly like a
    slope from a trend until you look at how much variance it explains.

    Returns ``(0.0, mean, 0.0)`` when x has no spread, which is the honest
    answer for "all the readings arrived at the same instant".
    """
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0.0
    if n != len(ys):  # pragma: no cover - guarded by callers
        raise ValueError("least_squares needs equal-length inputs")

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    variance_x = sum((x - mean_x) ** 2 for x in xs)
    if variance_x == 0.0:
        return 0.0, mean_y, 0.0

    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = covariance / variance_x
    intercept = mean_y - slope * mean_x

    total = sum((y - mean_y) ** 2 for y in ys)
    if total == 0.0:
        # Every y identical: the line fits perfectly and explains nothing.
        # Reporting 1.0 would let a flat battery reading masquerade as a
        # well-fitted trend, so this is deliberately 0.
        return slope, intercept, 0.0

    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    return slope, intercept, 1.0 - residual / total


def busiest_band(
    counts: Sequence[int], *, coverage: float = ACTIVE_BAND_COVERAGE
) -> tuple[int, int] | None:
    """The shortest run of hours containing ``coverage`` of all activity.

    Treated as a circle, so somebody whose day runs 21:00-02:00 gets that
    band rather than "00:00 to 23:00" -- which is what any non-wrapping
    version returns for an evening person, and is worse than saying nothing.

    ``counts`` is 24 hourly totals. Returns inclusive ``(start, end)`` hours,
    or ``None`` when there is no activity at all.
    """
    if len(counts) != 24:  # pragma: no cover - callers pass a full day
        raise ValueError("busiest_band expects 24 hourly counts")

    total = sum(counts)
    if total == 0:
        return None

    needed = total * coverage
    best: tuple[int, int, int] | None = None  # (length, start, end)

    for start in range(24):
        running = 0
        for length in range(1, 25):
            running += counts[(start + length - 1) % 24]
            if running >= needed:
                if best is None or length < best[0]:
                    best = (length, start, (start + length - 1) % 24)
                break

    if best is None:  # pragma: no cover - unreachable while total > 0
        return None
    return best[1], best[2]


def percentage_change(previous: int, current: int) -> float | None:
    """Fractional change from ``previous`` to ``current``.

    ``None`` when the baseline is zero: going from nothing to something is a
    real event, but it is not a percentage, and dividing by zero to get one
    produces an infinity that renders as nonsense.
    """
    if previous <= 0:
        return None
    return (current - previous) / previous


def support_confidence(*, sample_size: int, days_observed: int) -> Confidence:
    """How much to trust a habit claim, from how much was seen.

    Deliberately coarse. A number to two decimal places would imply a
    calibration that nothing here has done.
    """
    if days_observed >= 14 and sample_size >= 200:
        return "high"
    if days_observed >= 7 and sample_size >= 60:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def active_hours_insight(hourly: Sequence[int], *, days_observed: int) -> Insight | None:
    """When somebody is usually around."""
    total = sum(hourly)
    if days_observed < MIN_DAYS_FOR_HABIT or total < MIN_EVENTS_FOR_HABIT:
        return None

    band = busiest_band(hourly)
    if band is None:  # pragma: no cover - total > 0 guarantees a band
        return None

    start, end = band
    # A band covering almost the whole day is technically true and says
    # nothing. Reporting it would be worse than reporting nothing.
    span = (end - start) % 24 + 1
    if span >= 18:
        return None

    share = round(_band_share(hourly, start, end) * 100)
    return Insight(
        kind="active_hours",
        headline=f"You're usually around between {start:02d}:00 and {end + 1:02d}:00",
        detail=(
            f"{share}% of the times NOVA noticed you fell in that window, "
            f"across {days_observed} days."
        ),
        confidence=support_confidence(sample_size=total, days_observed=days_observed),
        sample_size=total,
        days_observed=days_observed,
    )


def busiest_weekday_insight(
    totals_by_weekday: dict[int, int],
    occurrences_by_weekday: dict[int, int],
    *,
    days_observed: int,
) -> Insight | None:
    """Which day of the week is busiest, per occurrence of that day.

    Per occurrence, not in total. A 17-day window contains three Mondays and
    two Saturdays, so raw totals would make Monday look busier than Saturday
    on nothing but the calendar.
    """
    if days_observed < MIN_DAYS_FOR_WEEKDAY:
        return None

    averages = {
        weekday: totals_by_weekday.get(weekday, 0) / occurrences
        for weekday, occurrences in occurrences_by_weekday.items()
        if occurrences >= MIN_OCCURRENCES_PER_WEEKDAY
    }
    # Fewer than two comparable days is not a comparison.
    if len(averages) < 2:
        return None

    busiest = max(averages, key=lambda day: averages[day])
    quietest = min(averages, key=lambda day: averages[day])
    if averages[busiest] == averages[quietest]:
        return None

    total = sum(totals_by_weekday.values())
    return Insight(
        kind="busiest_weekday",
        headline=f"{_WEEKDAYS[busiest]} is your busiest day",
        detail=(
            f"About {averages[busiest]:.0f} interactions on an average "
            f"{_WEEKDAYS[busiest]}, against {averages[quietest]:.0f} on "
            f"{_WEEKDAYS[quietest]}."
        ),
        confidence=support_confidence(sample_size=total, days_observed=days_observed),
        sample_size=total,
        days_observed=days_observed,
    )


def weekly_trend_insight(
    *, previous_week: int, current_week: int, days_observed: int
) -> Insight | None:
    """Whether this week looks different from last."""
    if min(previous_week, current_week) < MIN_EVENTS_PER_WEEK:
        return None

    change = percentage_change(previous_week, current_week)
    if change is None or abs(change) < MIN_MEANINGFUL_CHANGE:
        return None

    direction = "more" if change > 0 else "less"
    return Insight(
        kind="weekly_trend",
        headline=f"You've been around {abs(change) * 100:.0f}% {direction} this week",
        detail=(
            f"{current_week} interactions in the last seven days, against "
            f"{previous_week} the week before."
        ),
        confidence=support_confidence(
            sample_size=previous_week + current_week, days_observed=days_observed
        ),
        sample_size=previous_week + current_week,
        days_observed=days_observed,
    )


def battery_insight(
    samples: Sequence[tuple[datetime, int]], *, days_observed: int
) -> Insight | None:
    """How long the battery has left, from its current discharge.

    Only the current discharge segment is used. Fitting a line through a
    series that includes a recharge would average a rise and a fall into a
    slope that describes neither.
    """
    segment = current_discharge_segment(samples)
    if len(segment) < MIN_BATTERY_SAMPLES:
        return None

    start_at = segment[0][0]
    hours = [(at - start_at).total_seconds() / 3600 for at, _ in segment]
    levels = [float(level) for _, level in segment]

    if hours[-1] * 60 < MIN_BATTERY_SPAN_MINUTES:
        return None

    slope, _, r_squared = least_squares(hours, levels)
    # Slope is percent per hour and has to be a fall. A poor fit means the
    # readings are not describing a trend, and extrapolating one anyway is
    # how a projection becomes a fabrication.
    if slope >= 0 or r_squared < MIN_BATTERY_FIT_R2:
        return None

    current = levels[-1]
    remaining_hours = current / -slope

    return Insight(
        kind="battery_runtime",
        headline=f"About {_duration(remaining_hours)} of battery left",
        detail=(
            f"Dropping {-slope:.1f}% an hour over the last "
            f"{_duration(hours[-1])}, from {len(segment)} readings "
            f"(R²={r_squared:.2f})."
        ),
        confidence="high" if r_squared >= 0.95 else "medium",
        sample_size=len(segment),
        days_observed=days_observed,
    )


def connectivity_insight(
    *, longest_gap_seconds: int | None, gap_count: int, days_observed: int
) -> Insight | None:
    """Whether the device has been dropping off.

    Phrased as observed silences rather than an uptime percentage. A
    percentage needs to know how long the device was *meant* to be reachable,
    and nothing here does -- a NOVA switched off overnight on purpose would
    otherwise be scored as unreliable.
    """
    if longest_gap_seconds is None or gap_count == 0:
        return None

    return Insight(
        kind="connectivity",
        headline=f"NOVA dropped off {gap_count} time{'s' if gap_count != 1 else ''}",
        detail=(
            f"The longest silence was {_duration(longest_gap_seconds / 3600)}. "
            "This counts gaps between heartbeats, so a deliberate power-off "
            "looks the same as a lost connection."
        ),
        confidence="high" if gap_count >= 3 else "low",
        sample_size=gap_count,
        days_observed=days_observed,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def current_discharge_segment(
    samples: Sequence[tuple[datetime, int]],
) -> list[tuple[datetime, int]]:
    """The readings since the battery last stopped rising.

    A rise beyond :data:`CHARGE_TOLERANCE_PERCENT` **above the segment's
    lowest reading** starts a new segment; smaller rises are gauge noise and
    are kept, because splitting on every wobble would leave segments too
    short to fit.

    Measured against the running minimum rather than against the previous
    sample, and the difference is not academic. Comparing neighbours only
    catches a charge that arrives as a jump, which is an artefact of sampling
    slowly. A device heartbeating every 30 seconds sees a 15%/hour charge as
    0.125% per sample -- a rise of at most one point, forever under any sane
    tolerance. The charge is then never detected, the fit runs across a rise
    and a fall, and the R² gate rejects the result: the projection stops
    appearing at all, and nothing says why. That is what the firmware's own
    heartbeat interval produces, so it was not a hypothetical.

    Against the running minimum the same charge crosses the tolerance after
    about 3% of climb, however often it was sampled.
    """
    if not samples:
        return []

    segment: list[tuple[datetime, int]] = [samples[0]]
    floor = samples[0][1]

    for _, current in itertools.pairwise(samples):
        if current[1] - floor > CHARGE_TOLERANCE_PERCENT:
            # Climbing away from the trough: a charge, not a wobble.
            segment = [current]
            floor = current[1]
        else:
            segment.append(current)
            floor = min(floor, current[1])
    return segment


def _band_share(hourly: Sequence[int], start: int, end: int) -> float:
    """The share of activity falling inside an inclusive, wrapping band."""
    total = sum(hourly)
    if total == 0:  # pragma: no cover - callers check first
        return 0.0

    span = (end - start) % 24 + 1
    inside = sum(hourly[(start + offset) % 24] for offset in range(span))
    return inside / total


def _duration(hours: float) -> str:
    """Render a duration the way a person would say it."""
    if hours < 1:
        return f"{round(hours * 60)} minutes"
    if hours < 48:
        whole = int(hours)
        minutes = round((hours - whole) * 60)
        if minutes == 60:
            whole, minutes = whole + 1, 0
        if minutes == 0:
            return f"{whole} hour{'s' if whole != 1 else ''}"
        return f"{whole}h {minutes}m"
    return f"{round(hours / 24)} days"
