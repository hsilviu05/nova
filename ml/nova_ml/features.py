"""Labels and features, on a grid of decision points, from the past only.

Two ideas carry this module.

**Decision points on a grid, not one per event.** Predicting at every
telemetry row would hand a chatty device more samples than a quiet one and
make consecutive rows near-duplicates. Instead the span is walked at a fixed
step, and at each point the question is asked once: given everything up to
now, does an interaction happen in the next ten minutes?

**Nothing at a decision point can see past it.** Every feature is computed
from events at or before ``t``; the label is computed from events strictly
after it. That is not a convention -- it is enforced by ``searchsorted``
against a sorted time array, and ``tests/test_features.py`` checks that
features at ``t`` are byte-identical whether or not any later events exist.
Leakage does not show up in the metrics as a failure. It shows up as a
better score, which is why it has to be a test.

Rows in the final ``HORIZON`` of the observed span have no label -- their
future was never observed -- and are dropped rather than labelled "no".
Labelling them "no" would teach the model that the end of every dataset is
quiet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
from numpy.typing import NDArray

from nova_ml import HORIZON_SECONDS
from nova_ml.dataset import SOURCE_TELEMETRY, Dataset

# Decision points every five minutes. Finer than the horizon so the model
# sees the run-up to an interaction; coarse enough that a week is two
# thousand rows rather than twenty thousand near-copies.
GRID_STEP_SECONDS = 5 * 60

# Beyond this, "how long since" carries no more information and the raw
# number would dominate a linear model. A day.
SINCE_CAP_SECONDS = 24 * 60 * 60

# The VL53L0X reports nothing beyond about 1.2 m and the protocol caps the
# field at 400. An absent reading is filled with the cap and flagged, so
# "nobody in range" and "no sensor" are both representable.
DISTANCE_FILL_CM = 400.0

# Device states, grouped by what they say about attention. The behaviour
# engine has ten; three groups are what a few weeks of data can support.
ENGAGED_STATES = frozenset({"listening", "thinking", "speaking", "curious", "happy"})
ASLEEP_STATES = frozenset({"sleeping"})
OFFLINE_STATES = frozenset({"offline"})

FEATURE_NAMES: tuple[str, ...] = (
    "had_interaction",
    "seconds_since_last_interaction",
    "interactions_10m",
    "interactions_60m",
    "interactions_24h",
    "seconds_since_last_event",
    "distance_known",
    "last_distance_cm",
    "state_engaged",
    "state_asleep",
    "state_offline",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
)


@dataclass(frozen=True, slots=True)
class Frame:
    """A design matrix with its labels and the time of every row."""

    t: NDArray[np.float64]
    X: NDArray[np.float64]
    y: NDArray[np.int8]
    feature_names: tuple[str, ...]

    def __len__(self) -> int:
        return int(self.t.shape[0])

    @property
    def positive_rate(self) -> float:
        return float(self.y.mean()) if len(self) else 0.0


def decision_points(dataset: Dataset, step: int = GRID_STEP_SECONDS) -> NDArray[np.float64]:
    """Times to predict at: a grid from the first event to the last one that
    still has a full horizon of observed future."""
    if len(dataset) == 0:
        return np.empty(0, dtype=np.float64)
    start = dataset.t[0]
    last_labelable = dataset.t[-1] - HORIZON_SECONDS
    if last_labelable < start:
        return np.empty(0, dtype=np.float64)
    count = int((last_labelable - start) // step) + 1
    return np.asarray(start + step * np.arange(count, dtype=np.float64), dtype=np.float64)


def labels(dataset: Dataset, points: NDArray[np.float64]) -> NDArray[np.int8]:
    """1 if an interaction falls in (t, t + HORIZON], else 0.

    Open at ``t``: an interaction happening at the decision point itself is
    the present, and the model already sees it in the history features.
    """
    interaction_times = dataset.t[dataset.is_interaction]
    after = np.searchsorted(interaction_times, points, side="right")
    within = np.searchsorted(interaction_times, points + HORIZON_SECONDS, side="right")
    return (within > after).astype(np.int8)


Pair = tuple[NDArray[np.float64], NDArray[np.float64]]


def _cyclic(value: NDArray[np.float64], period: float) -> Pair:
    angle = 2.0 * np.pi * value / period
    return np.sin(angle), np.cos(angle)


def _local_clock(points: NDArray[np.float64], timezone: str) -> Pair:
    """Hour of day and day of week in the user's timezone.

    Per row through ``datetime`` rather than arithmetic on the epoch value,
    because DST moves the local hour by one twice a year and the analytics
    phase already learned that lesson.
    """
    zone = ZoneInfo(timezone)
    hours = np.empty(points.shape[0], dtype=np.float64)
    weekdays = np.empty(points.shape[0], dtype=np.float64)
    for index, stamp in enumerate(points):
        local = datetime.fromtimestamp(float(stamp), tz=UTC).astimezone(zone)
        hours[index] = local.hour + local.minute / 60.0
        weekdays[index] = float(local.weekday())
    return hours, weekdays


def features(
    dataset: Dataset, points: NDArray[np.float64], timezone: str = "UTC"
) -> NDArray[np.float64]:
    """The design matrix, one row per decision point, from the past only."""
    n = points.shape[0]
    X = np.zeros((n, len(FEATURE_NAMES)), dtype=np.float64)
    if n == 0:
        return X
    col = {name: i for i, name in enumerate(FEATURE_NAMES)}

    interaction_times = dataset.t[dataset.is_interaction]
    # Events at or before t: side="right" so an event exactly at t counts as
    # known. Nothing after t is ever indexed.
    seen = np.searchsorted(interaction_times, points, side="right")

    had = seen > 0
    X[:, col["had_interaction"]] = had
    since = np.full(n, SINCE_CAP_SECONDS, dtype=np.float64)
    since[had] = points[had] - interaction_times[seen[had] - 1]
    X[:, col["seconds_since_last_interaction"]] = np.minimum(since, SINCE_CAP_SECONDS)

    windows = (("interactions_10m", 600), ("interactions_60m", 3600), ("interactions_24h", 86400))
    for name, window in windows:
        before_window = np.searchsorted(interaction_times, points - window, side="right")
        X[:, col[name]] = seen - before_window

    any_seen = np.searchsorted(dataset.t, points, side="right")
    since_event = np.full(n, SINCE_CAP_SECONDS, dtype=np.float64)
    has_event = any_seen > 0
    since_event[has_event] = points[has_event] - dataset.t[any_seen[has_event] - 1]
    X[:, col["seconds_since_last_event"]] = np.minimum(since_event, SINCE_CAP_SECONDS)

    # Last known distance and state: walk telemetry rows that carry each, and
    # take the most recent at or before t.
    telemetry = dataset.source == SOURCE_TELEMETRY
    with_distance = telemetry & ~np.isnan(dataset.distance_cm)
    distance_times = dataset.t[with_distance]
    distance_values = dataset.distance_cm[with_distance]
    idx = np.searchsorted(distance_times, points, side="right")
    known = idx > 0
    X[:, col["distance_known"]] = known
    last_distance = np.full(n, DISTANCE_FILL_CM, dtype=np.float64)
    last_distance[known] = distance_values[idx[known] - 1]
    X[:, col["last_distance_cm"]] = last_distance

    with_state = telemetry & (dataset.state != "")
    state_times = dataset.t[with_state]
    state_values = dataset.state[with_state]
    idx = np.searchsorted(state_times, points, side="right")
    known = idx > 0
    last_state = np.full(n, "", dtype=state_values.dtype if state_values.size else np.str_)
    if state_values.size:
        last_state[known] = state_values[idx[known] - 1]
    X[:, col["state_engaged"]] = np.isin(last_state, list(ENGAGED_STATES))
    X[:, col["state_asleep"]] = np.isin(last_state, list(ASLEEP_STATES))
    X[:, col["state_offline"]] = np.isin(last_state, list(OFFLINE_STATES))

    hours, weekdays = _local_clock(points, timezone)
    X[:, col["hour_sin"]], X[:, col["hour_cos"]] = _cyclic(hours, 24.0)
    X[:, col["weekday_sin"]], X[:, col["weekday_cos"]] = _cyclic(weekdays, 7.0)

    return X


def build(dataset: Dataset, timezone: str = "UTC", step: int = GRID_STEP_SECONDS) -> Frame:
    points = decision_points(dataset, step)
    return Frame(
        t=points,
        X=features(dataset, points, timezone),
        y=labels(dataset, points),
        feature_names=FEATURE_NAMES,
    )
