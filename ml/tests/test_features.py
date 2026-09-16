from __future__ import annotations

import numpy as np

from nova_ml import HORIZON_SECONDS
from nova_ml.dataset import Dataset
from nova_ml.features import (
    DISTANCE_FILL_CM,
    FEATURE_NAMES,
    GRID_STEP_SECONDS,
    SINCE_CAP_SECONDS,
    build,
    decision_points,
    features,
    labels,
)
from tests.conftest import dataset, heartbeat, message, presence

COL = {name: i for i, name in enumerate(FEATURE_NAMES)}


def truncated(ds: Dataset, until: float) -> Dataset:
    """The same dataset with every event after ``until`` removed."""
    keep = ds.t <= until
    return Dataset(
        t=ds.t[keep],
        source=ds.source[keep],
        event_type=ds.event_type[keep],
        distance_cm=ds.distance_cm[keep],
        state=ds.state[keep],
        sha256=ds.sha256,
    )


class TestDecisionPoints:
    def test_grid_starts_at_the_first_event_and_steps_evenly(self) -> None:
        ds = dataset([heartbeat(0), heartbeat(60)])
        points = decision_points(ds)
        assert points[0] == ds.t[0]
        assert np.all(np.diff(points) == GRID_STEP_SECONDS)

    def test_the_last_horizon_is_dropped_not_labelled_quiet(self) -> None:
        # A row whose future was never observed has no label. Calling it
        # "no" teaches the model that every dataset ends quietly.
        ds = dataset([heartbeat(0), heartbeat(60)])
        points = decision_points(ds)
        assert np.all(points + HORIZON_SECONDS <= ds.t[-1])

    def test_a_dataset_shorter_than_the_horizon_has_no_points(self) -> None:
        ds = dataset([heartbeat(0), heartbeat(5)])
        assert decision_points(ds).size == 0
        assert decision_points(dataset([])).size == 0


class TestLabels:
    def test_interaction_inside_the_horizon_is_positive(self) -> None:
        ds = dataset([heartbeat(0), presence(5), heartbeat(60)])
        point = np.array([ds.t[0]])
        assert labels(ds, point).tolist() == [1]

    def test_interaction_exactly_at_the_horizon_is_positive(self) -> None:
        ds = dataset([heartbeat(0), presence(10), heartbeat(60)])
        assert labels(ds, np.array([ds.t[0]])).tolist() == [1]

    def test_interaction_just_past_the_horizon_is_negative(self) -> None:
        ds = dataset([heartbeat(0), presence(10.0 + 1 / 60), heartbeat(60)])
        assert labels(ds, np.array([ds.t[0]])).tolist() == [0]

    def test_interaction_at_the_decision_point_is_the_present_not_the_future(self) -> None:
        # It is already in the history features; counting it as the label
        # would make the model predict what it can see.
        ds = dataset([presence(0), heartbeat(60)])
        assert labels(ds, np.array([ds.t[0]])).tolist() == [0]

    def test_messages_are_interactions_too(self) -> None:
        ds = dataset([heartbeat(0), message(3), heartbeat(60)])
        assert labels(ds, np.array([ds.t[0]])).tolist() == [1]


class TestFeatures:
    def test_time_since_last_interaction_and_flag(self) -> None:
        ds = dataset([presence(0), heartbeat(60)])
        point = np.array([ds.t[0] + 900])  # 15 minutes later
        X = features(ds, point)
        assert X[0, COL["had_interaction"]] == 1
        assert X[0, COL["seconds_since_last_interaction"]] == 900

    def test_no_history_is_flagged_and_capped(self) -> None:
        ds = dataset([heartbeat(0), heartbeat(60)])
        X = features(ds, np.array([ds.t[0]]))
        assert X[0, COL["had_interaction"]] == 0
        assert X[0, COL["seconds_since_last_interaction"]] == SINCE_CAP_SECONDS

    def test_since_is_capped_at_a_day(self) -> None:
        ds = dataset([presence(0), heartbeat(60 * 72)])
        X = features(ds, np.array([ds.t[0] + 48 * 3600]))
        assert X[0, COL["seconds_since_last_interaction"]] == SINCE_CAP_SECONDS

    def test_window_counts_are_closed_on_the_left_and_at_t(self) -> None:
        # Interactions at -70, -30, -5 and 0 minutes; windows are (t-w, t].
        ds = dataset([presence(0), presence(40), presence(65), presence(70), heartbeat(120)])
        point = np.array([ds.t[3]])  # t = minute 70
        X = features(ds, point)
        assert X[0, COL["interactions_10m"]] == 2  # 65, 70
        assert X[0, COL["interactions_60m"]] == 3  # 40, 65, 70
        assert X[0, COL["interactions_24h"]] == 4

    def test_last_distance_is_the_most_recent_at_or_before_t(self) -> None:
        ds = dataset([presence(0, "90"), presence(5, "40"), presence(30, "20"), heartbeat(60)])
        X = features(ds, np.array([ds.t[1] + 60]))  # one minute after the 40
        assert X[0, COL["distance_known"]] == 1
        assert X[0, COL["last_distance_cm"]] == 40

    def test_unknown_distance_is_filled_with_the_cap_and_flagged(self) -> None:
        ds = dataset([heartbeat(0), heartbeat(60)])
        X = features(ds, np.array([ds.t[0]]))
        assert X[0, COL["distance_known"]] == 0
        assert X[0, COL["last_distance_cm"]] == DISTANCE_FILL_CM

    def test_state_groups(self) -> None:
        ds = dataset(
            [
                heartbeat(0, "sleeping"),
                heartbeat(10, "listening"),
                heartbeat(20, "offline"),
                heartbeat(60),
            ]
        )
        points = np.array([ds.t[0], ds.t[1], ds.t[2]])
        X = features(ds, points)
        assert X[:, COL["state_asleep"]].tolist() == [1, 0, 0]
        assert X[:, COL["state_engaged"]].tolist() == [0, 1, 0]
        assert X[:, COL["state_offline"]].tolist() == [0, 0, 1]

    def test_hour_of_day_follows_the_users_timezone(self) -> None:
        # 08:00 UTC is 11:00 in Bucharest in September. Same instant,
        # different angle -- so the feature depends on the timezone, and
        # getting it wrong shifts every habit by three hours.
        ds = dataset([heartbeat(0), heartbeat(60)])
        point = np.array([ds.t[0]])
        utc = features(ds, point, "UTC")
        local = features(ds, point, "Europe/Bucharest")
        assert not np.isclose(utc[0, COL["hour_sin"]], local[0, COL["hour_sin"]])

    def test_matrix_has_no_nan(self) -> None:
        ds = dataset([heartbeat(0), presence(3, ""), heartbeat(90, "")])
        frame = build(ds)
        assert not np.isnan(frame.X).any()


class TestNoLookahead:
    def test_features_are_identical_with_the_future_removed(self) -> None:
        """The leakage detector.

        For each decision point, compute features on the full dataset and on
        the dataset with everything after that point deleted. The two must
        match byte for byte. Any feature that peeks forward fails this, and
        no metric would ever have caught it -- leakage makes scores better.
        """
        rows = [
            heartbeat(m, "idle" if m % 3 else "listening", str(30 + m % 50))
            for m in range(0, 600, 7)
        ]
        rows += [presence(m) for m in range(4, 600, 23)]
        rows += [message(m) for m in range(50, 600, 97)]
        ds = dataset(rows)
        points = decision_points(ds)
        full = features(ds, points, "Europe/Bucharest")

        for i in range(0, points.size, 3):
            past_only = truncated(ds, points[i])
            partial = features(past_only, points[i : i + 1], "Europe/Bucharest")
            assert np.array_equal(full[i], partial[0]), f"feature leaked at point {i}"

    def test_labels_depend_only_on_the_future(self) -> None:
        # The mirror image: deleting the past must not change a label.
        rows = [heartbeat(m) for m in range(0, 300, 10)] + [presence(m) for m in (33, 121, 202)]
        ds = dataset(rows)
        points = decision_points(ds)
        full = labels(ds, points)
        for i in range(points.size):
            keep = ds.t >= points[i]
            future_only = Dataset(
                t=ds.t[keep],
                source=ds.source[keep],
                event_type=ds.event_type[keep],
                distance_cm=ds.distance_cm[keep],
                state=ds.state[keep],
                sha256=ds.sha256,
            )
            assert labels(future_only, points[i : i + 1])[0] == full[i]
