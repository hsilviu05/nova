"""The dataset contract: what a row is, and what counts as an interaction.

The pipeline reads one CSV, exported from the API's database by
``services/api/scripts/export_dataset.py``. The contract is deliberately
narrow so that the ML side never imports the API and the file is a
self-contained, hashable artefact: a trained model records the SHA-256 of the
file it was trained on, and that is the whole provenance chain.

Columns, in order:

    recorded_at   ISO 8601, UTC. The device's own clock for telemetry, the
                  server's for messages.
    source        "telemetry" or "message".
    event_type    The telemetry event type, or "user_message".
    distance_cm   Integer, or empty when the event carried no reading.
    state         The device state string, or empty.

**What is an interaction.** A ``person_detected`` telemetry event -- the
time-of-flight sensor noticing somebody within range -- or a message the user
sent from the phone. Heartbeats and state changes are context, not
interactions: a device reporting that it is alive says nothing about whether
anyone is there.

That definition is a modelling choice and it is recorded here rather than
buried in a query, because the label depends on it and so does every number
the evaluation produces.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

COLUMNS = ("recorded_at", "source", "event_type", "distance_cm", "state")

SOURCE_TELEMETRY = "telemetry"
SOURCE_MESSAGE = "message"

INTERACTION_EVENT_TYPES = frozenset({"person_detected"})
MESSAGE_EVENT_TYPE = "user_message"


class DatasetError(ValueError):
    """The file does not meet the contract. Named so a caller can tell a bad
    export from a bad model."""


@dataclass(frozen=True, slots=True)
class Dataset:
    """Events in chronological order, as parallel arrays.

    ``t`` is seconds since the Unix epoch as float64. Everything downstream
    is a ``searchsorted`` against it, which is why it is an array and not a
    list of datetimes.
    """

    t: NDArray[np.float64]
    source: NDArray[np.str_]
    event_type: NDArray[np.str_]
    distance_cm: NDArray[np.float64]  # NaN where absent
    state: NDArray[np.str_]  # "" where absent
    sha256: str

    def __len__(self) -> int:
        return int(self.t.shape[0])

    @property
    def is_interaction(self) -> NDArray[np.bool_]:
        telemetry_hit = (self.source == SOURCE_TELEMETRY) & np.isin(
            self.event_type, list(INTERACTION_EVENT_TYPES)
        )
        message_hit = self.source == SOURCE_MESSAGE
        return np.asarray(telemetry_hit | message_hit, dtype=np.bool_)

    @property
    def span_seconds(self) -> float:
        if len(self) == 0:
            return 0.0
        return float(self.t[-1] - self.t[0])


def _parse_time(text: str, line: int) -> float:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise DatasetError(f"line {line}: recorded_at {text!r} is not ISO 8601") from error
    if parsed.tzinfo is None:
        # A naive timestamp is ambiguous by up to a day across timezones, and
        # the hour-of-day feature would silently absorb the error.
        raise DatasetError(f"line {line}: recorded_at {text!r} has no timezone")
    return parsed.astimezone(UTC).timestamp()


def parse(text: str) -> Dataset:
    """Parse CSV text against the contract.

    Rows are sorted by time here rather than trusted to arrive sorted: the
    export orders them, but a hand-edited file would not, and every feature
    assumes the order.
    """
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None or tuple(reader.fieldnames) != COLUMNS:
        raise DatasetError(f"expected columns {list(COLUMNS)}, got {reader.fieldnames}")

    times: list[float] = []
    sources: list[str] = []
    events: list[str] = []
    distances: list[float] = []
    states: list[str] = []

    for line, row in enumerate(reader, start=2):
        source = row["source"]
        if source not in (SOURCE_TELEMETRY, SOURCE_MESSAGE):
            raise DatasetError(f"line {line}: unknown source {source!r}")
        event_type = row["event_type"]
        if not event_type:
            raise DatasetError(f"line {line}: empty event_type")

        times.append(_parse_time(row["recorded_at"], line))
        sources.append(source)
        events.append(event_type)
        raw_distance = row["distance_cm"]
        if raw_distance == "":
            distances.append(np.nan)
        else:
            try:
                distances.append(float(int(raw_distance)))
            except ValueError as error:
                raise DatasetError(
                    f"line {line}: distance_cm {raw_distance!r} is not an integer"
                ) from error
        states.append(row["state"])

    order = np.argsort(np.asarray(times, dtype=np.float64), kind="stable")
    return Dataset(
        t=np.asarray(times, dtype=np.float64)[order],
        source=np.asarray(sources, dtype=np.str_)[order],
        event_type=np.asarray(events, dtype=np.str_)[order],
        distance_cm=np.asarray(distances, dtype=np.float64)[order],
        state=np.asarray(states, dtype=np.str_)[order],
        sha256=sha256,
    )


def load(path: Path) -> Dataset:
    return parse(path.read_text(encoding="utf-8"))
