from __future__ import annotations

import numpy as np
import pytest

from nova_ml import HORIZON_SECONDS
from nova_ml.features import FEATURE_NAMES, GRID_STEP_SECONDS, Frame
from nova_ml.split import (
    MIN_POSITIVES,
    MIN_ROWS,
    MIN_SPAN_SECONDS,
    MIN_TEST_POSITIVES,
    gate,
    temporal_split,
)


def frame(rows: int, positive_every: int = 20, step: int = GRID_STEP_SECONDS) -> Frame:
    """A frame with a regular label pattern. Arrays only; nothing is modelled."""
    t = 1_700_000_000.0 + step * np.arange(rows, dtype=np.float64)
    y = np.zeros(rows, dtype=np.int8)
    if positive_every > 0:
        y[::positive_every] = 1
    return Frame(t=t, X=np.zeros((rows, len(FEATURE_NAMES))), y=y, feature_names=FEATURE_NAMES)


class TestTemporalSplit:
    def test_periods_are_in_order_and_disjoint(self) -> None:
        f = frame(1000)
        s = temporal_split(f)
        assert f.t[s.train].max() < f.t[s.validation].min() < f.t[s.test].min()
        assert not set(s.train) & set(s.validation)
        assert not set(s.validation) & set(s.test)

    def test_no_training_label_crosses_into_validation(self) -> None:
        # The embargo. A training row's label looks HORIZON into its
        # future; if that reaches the validation period, the model was
        # shown validation outcomes during training.
        f = frame(1000)
        s = temporal_split(f)
        assert np.all(f.t[s.train] + HORIZON_SECONDS <= f.t[s.validation].min())
        assert np.all(f.t[s.validation] + HORIZON_SECONDS <= f.t[s.test].min())

    def test_exactly_the_crossing_rows_are_dropped(self) -> None:
        # Five-minute grid, ten-minute horizon: the row one step before each
        # boundary crosses it, the row two steps before lands exactly on it
        # and is kept. One row per boundary, two boundaries.
        s = temporal_split(frame(1000))
        assert s.embargoed == 2

    def test_no_embargo_drops_nothing(self) -> None:
        s = temporal_split(frame(1000), embargo_seconds=0)
        assert s.embargoed == 0
        assert s.train.size + s.validation.size + s.test.size == 1000

    def test_rejects_bad_fractions(self) -> None:
        with pytest.raises(ValueError):
            temporal_split(frame(100), train_fraction=0.7, validation_fraction=0.3)
        with pytest.raises(ValueError):
            temporal_split(frame(100), train_fraction=0.0)

    def test_rejects_an_unsorted_frame(self) -> None:
        f = frame(10)
        shuffled = Frame(t=f.t[::-1].copy(), X=f.X, y=f.y, feature_names=f.feature_names)
        with pytest.raises(ValueError, match="chronological"):
            temporal_split(shuffled)

    def test_empty_frame(self) -> None:
        s = temporal_split(frame(0))
        assert s.train.size == s.validation.size == s.test.size == 0


class TestGates:
    def test_enough_data_passes(self) -> None:
        # A little over 14 days at five minutes, 5% positive. The span is
        # first-to-last, so exactly 14 days of steps falls one step short.
        f = frame(4100)
        g = gate(f, temporal_split(f))
        assert g.ok, g.reasons

    def test_too_few_rows(self) -> None:
        f = frame(MIN_ROWS - 1)
        g = gate(f, temporal_split(f))
        assert not g.ok
        assert any("decision points" in r for r in g.reasons)

    def test_too_short_a_span(self) -> None:
        # Plenty of rows, crammed into a day.
        f = frame(4032, step=20)
        g = gate(f, temporal_split(f))
        assert any("spans" in r for r in g.reasons)
        assert MIN_SPAN_SECONDS > 4032 * 20

    def test_too_few_positives(self) -> None:
        f = frame(4032, positive_every=100)  # ~40 positives
        g = gate(f, temporal_split(f))
        assert MIN_POSITIVES > 4032 // 100
        assert any("positive examples" in r for r in g.reasons)

    def test_positive_rate_too_low(self) -> None:
        f = frame(20000, positive_every=100)  # 200 positives but 1%
        g = gate(f, temporal_split(f))
        assert any("anomaly detection" in r for r in g.reasons)

    def test_test_set_needs_its_own_positives(self) -> None:
        # Positives only in the first half: overall count is fine, but the
        # test period has none, so no metric on it means anything.
        f = frame(4032, positive_every=0)
        y = f.y.copy()
        y[:2000:20] = 1
        f = Frame(t=f.t, X=f.X, y=y, feature_names=f.feature_names)
        g = gate(f, temporal_split(f))
        assert any("test set has" in r and f"need {MIN_TEST_POSITIVES}" in r for r in g.reasons)

    def test_a_set_with_no_negatives_is_refused(self) -> None:
        f = frame(4032, positive_every=1)
        g = gate(f, temporal_split(f))
        assert any("no negatives" in r for r in g.reasons)

    def test_every_reason_is_named(self) -> None:
        # The gate is a list of sentences, not a boolean: whoever runs the
        # pipeline on thin data is told exactly what is thin.
        f = frame(10)
        g = gate(f, temporal_split(f))
        assert not g.ok
        assert len(g.reasons) >= 3
        assert all(r for r in g.reasons)
