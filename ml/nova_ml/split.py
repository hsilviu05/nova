"""A chronological split with an embargo, and the gates that refuse to train.

ADR 005 settles the first half: train on the earliest period, validate on
the middle, test on the latest, never shuffle. This module adds the part the
ADR does not spell out.

**The embargo.** A row's label looks ten minutes into its future. The last
training rows before the validation boundary therefore have labels computed
from events that are *inside* the validation period -- the model is shown,
during training, what happens at the start of the data it is about to be
scored on. The fix is to drop every training row whose label window crosses
the boundary, and the same again between validation and test. It costs ten
minutes of rows per boundary and removes a leak that is invisible in the
metrics, where it appears as a small, flattering improvement.

**The gates.** The insight engine in the analytics phase refuses to claim a
habit it cannot support. The same rule applies with more force here, because
a trained model with a reported ROC-AUC looks far more authoritative than a
sentence. Below the thresholds the pipeline exits with a named reason. It
does not produce a model with a caveat attached; caveats get lost and
numbers get quoted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from nova_ml import HORIZON_SECONDS
from nova_ml.features import Frame

# -- gates ------------------------------------------------------------------
#
# Chosen for what the reported number claims, not for what makes a demo run.

# Decision points. At a five-minute grid this is about a week of data, which
# is the least that lets the weekday features mean anything at all.
MIN_ROWS = 1500
# Span of the data. The weekday features need every weekday to have come
# round at least twice, or the model learns one particular Tuesday.
MIN_SPAN_SECONDS = 14 * 24 * 60 * 60
# Positive examples overall and in the test set. Precision and recall on a
# handful of positives are noise with a decimal point.
MIN_POSITIVES = 60
MIN_TEST_POSITIVES = 15
# Below this share the problem is anomaly detection wearing a classifier's
# clothes, and a different method is called for.
MIN_POSITIVE_RATE = 0.02


@dataclass(frozen=True, slots=True)
class Split:
    train: NDArray[np.intp]
    validation: NDArray[np.intp]
    test: NDArray[np.intp]
    embargoed: int
    """Rows dropped at the two boundaries. Reported, so a suspiciously small
    training set has an explanation."""


@dataclass(frozen=True, slots=True)
class Gate:
    ok: bool
    reasons: tuple[str, ...]


def temporal_split(
    frame: Frame,
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
    embargo_seconds: int = HORIZON_SECONDS,
) -> Split:
    """Chronological indices, with rows whose labels cross a boundary removed."""
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("fractions must be in (0, 1)")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train + validation must leave room for a test set")

    n = len(frame)
    t = frame.t
    if n == 0:
        empty = np.empty(0, dtype=np.intp)
        return Split(empty, empty, empty, 0)
    if np.any(np.diff(t) < 0):
        raise ValueError("frame is not in chronological order")

    train_end = int(n * train_fraction)
    validation_end = int(n * (train_fraction + validation_fraction))

    validation_start_t = t[train_end] if train_end < n else np.inf
    test_start_t = t[validation_end] if validation_end < n else np.inf

    all_index = np.arange(n, dtype=np.intp)
    train = all_index[:train_end]
    validation = all_index[train_end:validation_end]
    test = all_index[validation_end:]

    # A row with t + horizon > next period's first t has a label that was
    # computed from the next period. Drop it from the earlier side.
    keep_train = t[train] + embargo_seconds <= validation_start_t
    keep_validation = t[validation] + embargo_seconds <= test_start_t
    embargoed = int((~keep_train).sum() + (~keep_validation).sum())

    return Split(
        train=train[keep_train],
        validation=validation[keep_validation],
        test=test,
        embargoed=embargoed,
    )


def gate(frame: Frame, split: Split) -> Gate:
    """Is there enough here to train on, and to believe the result?"""
    reasons: list[str] = []
    n = len(frame)

    if n < MIN_ROWS:
        reasons.append(f"{n} decision points, need at least {MIN_ROWS}")

    span = float(frame.t[-1] - frame.t[0]) if n else 0.0
    if span < MIN_SPAN_SECONDS:
        reasons.append(
            f"data spans {span / 86400:.1f} days, need at least {MIN_SPAN_SECONDS / 86400:.0f}"
        )

    positives = int(frame.y.sum()) if n else 0
    if positives < MIN_POSITIVES:
        reasons.append(f"{positives} positive examples, need at least {MIN_POSITIVES}")

    rate = frame.positive_rate
    if n and rate < MIN_POSITIVE_RATE:
        reasons.append(
            f"positive rate {rate:.1%} is below {MIN_POSITIVE_RATE:.0%}; "
            "this is anomaly detection, not classification"
        )

    for name, index, minimum in (
        ("train", split.train, 1),
        ("validation", split.validation, 1),
        ("test", split.test, MIN_TEST_POSITIVES),
    ):
        if index.size == 0:
            reasons.append(f"{name} set is empty")
            continue
        part = frame.y[index]
        if part.sum() < minimum:
            reasons.append(f"{name} set has {int(part.sum())} positives, need {minimum}")
        if minimum == 1 and part.sum() == part.size:
            reasons.append(f"{name} set has no negatives")

    return Gate(ok=not reasons, reasons=tuple(reasons))
