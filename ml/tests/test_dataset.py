from __future__ import annotations

import numpy as np
import pytest

from nova_ml.dataset import DatasetError, parse
from tests.conftest import csv_text, dataset, heartbeat, message, presence


class TestParsing:
    def test_rows_come_back_in_chronological_order(self) -> None:
        # Out of order on purpose: every feature assumes sorted time, and a
        # hand-edited export will not be.
        ds = dataset([heartbeat(30), presence(10), message(20)])
        assert np.all(np.diff(ds.t) > 0)
        assert list(ds.event_type) == ["person_detected", "user_message", "heartbeat"]

    def test_rejects_wrong_columns(self) -> None:
        with pytest.raises(DatasetError, match="expected columns"):
            parse("recorded_at,event_type\n2026-09-01T08:00:00+00:00,x\n")

    def test_rejects_a_naive_timestamp(self) -> None:
        # Ambiguous by up to a day across timezones; the hour feature would
        # silently absorb the error.
        text = (
            "recorded_at,source,event_type,distance_cm,state\n"
            "2026-09-01T08:00:00,telemetry,heartbeat,,\n"
        )
        with pytest.raises(DatasetError, match="no timezone"):
            parse(text)

    def test_rejects_an_unknown_source(self) -> None:
        with pytest.raises(DatasetError, match="unknown source"):
            dataset([(0, "camera", "frame", "", "")])

    def test_rejects_a_non_integer_distance(self) -> None:
        with pytest.raises(DatasetError, match="distance_cm"):
            dataset([(0, "telemetry", "person_detected", "near", "")])

    def test_absent_distance_is_nan_not_zero(self) -> None:
        # Zero centimetres is "touching the sensor"; absent is absent.
        ds = dataset([heartbeat(0)])
        assert np.isnan(ds.distance_cm[0])

    def test_empty_file_is_an_empty_dataset(self) -> None:
        ds = dataset([])
        assert len(ds) == 0
        assert ds.span_seconds == 0.0


class TestInteractions:
    def test_presence_and_messages_count_heartbeats_do_not(self) -> None:
        ds = dataset(
            [heartbeat(0), presence(1), message(2), (3, "telemetry", "device.state", "", "idle")]
        )
        assert list(ds.is_interaction) == [False, True, True, False]


class TestProvenance:
    def test_hash_is_stable_and_content_sensitive(self) -> None:
        a = csv_text([heartbeat(0)])
        b = csv_text([heartbeat(0)])
        c = csv_text([heartbeat(1)])
        assert parse(a).sha256 == parse(b).sha256
        assert parse(a).sha256 != parse(c).sha256
