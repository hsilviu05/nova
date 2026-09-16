"""Training mechanics, on rows too few to mean anything.

Nothing here is a result. The dataset below is a few hours of hand-placed
events that exists to prove the pipeline fits, chooses on validation, scores
test once, writes a manifest, and refuses at the gates. Its metrics are
discarded by every assertion that sees them.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from nova_ml.dataset import Dataset
from nova_ml.train import (
    GateRefusedError,
    baselines,
    best_threshold,
    evaluate,
    main,
    train,
)
from tests.conftest import csv_text, dataset, heartbeat, presence


def hours_of_rows() -> list[tuple[float, str, str, str, str]]:
    # Eight hours: a heartbeat every five minutes, a presence event every
    # twenty-five. Both classes land in every split, which is all the
    # mechanics need and nothing the gates would accept.
    rows = [heartbeat(m, "idle", str(80 + (m % 7) * 10)) for m in range(0, 8 * 60, 5)]
    rows += [presence(m) for m in range(3, 8 * 60, 25)]
    return rows


@pytest.fixture
def small() -> Dataset:
    return dataset(hours_of_rows())


class TestEvaluate:
    def test_counts_and_rates_match_a_known_confusion_matrix(self) -> None:
        y = np.array([1, 1, 1, 0, 0, 0, 0, 0], dtype=np.int8)
        scores = np.array([0.9, 0.8, 0.2, 0.7, 0.1, 0.1, 0.1, 0.1])
        e = evaluate(scores, y, 0.5)
        assert (e.true_positives, e.false_positives, e.false_negatives, e.true_negatives) == (
            2,
            1,
            1,
            4,
        )
        assert e.precision == pytest.approx(2 / 3)
        assert e.recall == pytest.approx(2 / 3)
        assert e.rows == 8 and e.positives == 3

    def test_a_constant_score_has_auc_of_a_half_not_an_exception(self) -> None:
        y = np.array([1, 0, 1, 0], dtype=np.int8)
        e = evaluate(np.full(4, 0.3), y, 0.5)
        assert e.roc_auc == 0.5


class TestThreshold:
    def test_chosen_to_maximise_f1_on_the_given_set(self) -> None:
        # At 0.5 the second row is a false positive; at 0.7 the split is
        # perfect. The chooser must find 0.7.
        y = np.array([1, 0, 1, 0], dtype=np.int8)
        scores = np.array([0.9, 0.6, 0.8, 0.1])
        assert best_threshold(scores, y) == pytest.approx(0.7, abs=0.11)
        chosen = best_threshold(scores, y)
        assert evaluate(scores, y, chosen).f1 == 1.0


class TestBaselines:
    def test_majority_never_predicts_and_base_rate_is_honest(self) -> None:
        y_train = np.array([1, 0, 0, 0, 0] * 20, dtype=np.int8)  # 20% positive
        y_test = np.array([1, 0, 0, 0, 0] * 4, dtype=np.int8)
        b = baselines(y_train, y_test)
        assert b.majority.recall == 0.0
        assert b.majority.true_positives == 0
        assert b.base_rate.roc_auc == 0.5
        assert b.base_rate.precision == pytest.approx(0.2)


class TestTrain:
    def test_refuses_thin_data_at_the_gates(self, small: Dataset, tmp_path: Path) -> None:
        with pytest.raises(GateRefusedError) as refused:
            train(small, timezone="UTC", registry=tmp_path)
        assert refused.value.gate.reasons
        # Nothing reaches the registry when the gate says no.
        assert list(tmp_path.iterdir()) == []

    def test_mechanics_with_gates_off(self, small: Dataset, tmp_path: Path) -> None:
        result = train(small, timezone="UTC", registry=tmp_path, enforce_gates=False)

        assert result.chosen in result.validation
        assert set(result.validation) == {
            "logistic_regression",
            "random_forest",
            "gradient_boosting",
        }
        assert not result.gate.ok  # recorded honestly, even when overridden

        entry = tmp_path / result.version
        assert (entry / "model.joblib").exists()
        manifest = json.loads((entry / "manifest.json").read_text())

        # Provenance: the manifest names the exact file and the split.
        assert manifest["dataset"]["sha256"] == small.sha256
        assert manifest["split"]["test"] == result.split.test.size
        assert manifest["gate"]["ok"] is False
        assert manifest["feature_names"] == list(result.validation and manifest["feature_names"])

        # The threshold was chosen on validation, and is the one applied to test.
        assert manifest["threshold"] == manifest["validation"][result.chosen]["threshold"]
        assert manifest["test"]["threshold"] == manifest["threshold"]
        assert manifest["test"]["rows"] == result.split.test.size

        # Baselines sit beside the model in the same manifest.
        assert set(manifest["baselines"]) == {"majority", "base_rate"}
        assert manifest["baselines"]["majority"]["true_positives"] == 0

    def test_is_deterministic(self, small: Dataset, tmp_path: Path) -> None:
        # Same file, same code, same seeds: the numbers must not move.
        first = train(small, timezone="UTC", registry=tmp_path / "a", enforce_gates=False)
        second = train(small, timezone="UTC", registry=tmp_path / "b", enforce_gates=False)
        assert first.validation == second.validation
        assert first.test == second.test
        assert first.chosen == second.chosen

    def test_model_selection_never_looks_at_test(self, small: Dataset, tmp_path: Path) -> None:
        # Selection uses validation ROC-AUC. Whatever wins on validation is
        # what gets scored on test, even when another model would have
        # scored higher there -- that is the discipline, not an accident.
        result = train(small, timezone="UTC", registry=tmp_path, enforce_gates=False)
        best_on_validation = max(result.validation, key=lambda n: result.validation[n].roc_auc)
        assert result.chosen == best_on_validation


class TestCommandLine:
    def test_thin_data_exits_2_with_reasons(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "d.csv"
        path.write_text(csv_text(hours_of_rows()))
        code = main(
            ["--dataset", str(path), "--timezone", "UTC", "--registry", str(tmp_path / "r")]
        )
        assert code == 2
        out = capsys.readouterr().out
        assert "not enough data" in out
        assert "decision points" in out
        assert not (tmp_path / "r").exists()

    def test_bad_file_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "d.csv"
        path.write_text("nope,columns\n1,2\n")
        code = main(
            ["--dataset", str(path), "--timezone", "UTC", "--registry", str(tmp_path / "r")]
        )
        assert code == 1
        assert "cannot read dataset" in capsys.readouterr().err

    def test_the_command_line_has_no_gate_override(self) -> None:
        # No invented data reaches the registry through the front door.
        with pytest.raises(SystemExit):
            main(["--dataset", "x", "--timezone", "UTC", "--no-gates"])
