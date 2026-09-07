"""Hand-built rows for proving the code.

Read the module docstring in ``nova_ml`` before adding to this file: these
rows exist to exercise labelling, windows, splits and gates. They are never
trained on, never scored, and no number derived from them is a result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nova_ml.dataset import Dataset, parse

BASE = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

Row = tuple[float, str, str, str, str]
"""(minutes after BASE, source, event_type, distance_cm, state)"""


def csv_text(rows: list[Row]) -> str:
    lines = ["recorded_at,source,event_type,distance_cm,state"]
    for minutes, source, event_type, distance, state in rows:
        stamp = (BASE + timedelta(minutes=minutes)).isoformat()
        lines.append(f"{stamp},{source},{event_type},{distance},{state}")
    return "\n".join(lines) + "\n"


def dataset(rows: list[Row]) -> Dataset:
    return parse(csv_text(rows))


def heartbeat(minutes: float, state: str = "idle", distance: str = "") -> Row:
    return (minutes, "telemetry", "heartbeat", distance, state)


def presence(minutes: float, distance: str = "60") -> Row:
    return (minutes, "telemetry", "person_detected", distance, "curious")


def message(minutes: float) -> Row:
    return (minutes, "message", "user_message", "", "")


@pytest.fixture
def base() -> datetime:
    return BASE
