"""Training, evaluation, and the registry.

Three models in increasing capacity, a validation set to choose between
them, and a test set that is looked at exactly once. The order matters:
choosing the model *and* the threshold on the test set is the second most
common way to inflate a behavioural score, after shuffling the split.

The evaluation reports what ADR 005 asks for -- precision, recall, F1,
ROC-AUC and the confusion matrix -- and puts two baselines beside them. The
majority baseline never predicts an interaction; the base-rate baseline
guesses at the training positive rate. A model that does not clearly beat
both has learned nothing, whatever its accuracy says.

Every trained model is written to the registry with a manifest recording the
dataset's SHA-256, its span and size, the feature set, the split, every
model's validation score, the chosen model's test score, the baselines, and
the library versions. A number quoted from this pipeline can be traced back
to the exact file and code that produced it.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from numpy.typing import NDArray
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from nova_ml import HORIZON_SECONDS
from nova_ml.dataset import Dataset
from nova_ml.features import build
from nova_ml.split import Gate, Split, gate, temporal_split

# Fixed seeds. A retrained model on the same file must produce the same
# manifest, or "reproducible" is a word rather than a property.
RANDOM_STATE = 0


def model_factories() -> dict[str, Any]:
    """The candidates, in the order the README promises.

    Logistic regression is scaled because it is the one model here that
    cares about feature magnitude, and "seconds since" is six orders larger
    than a sine. Class weights are balanced throughout: at a 5% positive
    rate an unweighted model learns to say "no" and is rewarded for it.
    """
    return {
        "logistic_regression": lambda: make_pipeline(
            StandardScaler(),
            LogisticRegression(class_weight="balanced", max_iter=2000, random_state=RANDOM_STATE),
        ),
        "random_forest": lambda: RandomForestClassifier(
            n_estimators=300,
            min_samples_leaf=5,
            class_weight="balanced_subsample",
            random_state=RANDOM_STATE,
            n_jobs=1,
        ),
        "gradient_boosting": lambda: HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            class_weight="balanced",
            random_state=RANDOM_STATE,
        ),
    }


@dataclass(frozen=True, slots=True)
class Evaluation:
    precision: float
    recall: float
    f1: float
    roc_auc: float
    true_negatives: int
    false_positives: int
    false_negatives: int
    true_positives: int
    threshold: float
    rows: int
    positives: int


def evaluate(scores: NDArray[np.float64], y: NDArray[np.int8], threshold: float) -> Evaluation:
    """Metrics at a threshold. ``scores`` are probabilities of the positive class."""
    predicted = (scores >= threshold).astype(np.int8)
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel()
    # ROC-AUC is undefined with one class present; the gates prevent that,
    # but the baselines can produce constant scores, and a constant score
    # is exactly 0.5 by definition rather than an error.
    auc = 0.5 if scores.min() == scores.max() else float(roc_auc_score(y, scores))
    return Evaluation(
        precision=float(precision_score(y, predicted, zero_division=0.0)),
        recall=float(recall_score(y, predicted, zero_division=0.0)),
        f1=float(f1_score(y, predicted, zero_division=0.0)),
        roc_auc=auc,
        true_negatives=int(tn),
        false_positives=int(fp),
        false_negatives=int(fn),
        true_positives=int(tp),
        threshold=threshold,
        rows=int(y.size),
        positives=int(y.sum()),
    )


def best_threshold(scores: NDArray[np.float64], y: NDArray[np.int8]) -> float:
    """The threshold that maximises F1 -- chosen on validation, never test."""
    candidates = np.unique(np.concatenate([[0.5], np.round(scores, 3)]))
    best, best_f1 = 0.5, -1.0
    for candidate in candidates:
        score = float(f1_score(y, (scores >= candidate).astype(np.int8), zero_division=0.0))
        if score > best_f1:
            best, best_f1 = float(candidate), score
    return best


@dataclass(frozen=True, slots=True)
class Baselines:
    """What nothing scores. Reported so the model's numbers have a floor."""

    majority: Evaluation
    base_rate: Evaluation


def baselines(y_train: NDArray[np.int8], y_test: NDArray[np.int8]) -> Baselines:
    rate = float(y_train.mean()) if y_train.size else 0.0
    # Majority: every score is 0, so nothing is ever predicted positive.
    majority = evaluate(np.zeros(y_test.size), y_test, 0.5)
    # Base rate: every score is the training rate. Precision is the rate,
    # recall is 1 if the threshold lets it through; ROC-AUC is 0.5.
    base = evaluate(np.full(y_test.size, rate), y_test, rate)
    return Baselines(majority=majority, base_rate=base)


class GateRefusedError(RuntimeError):
    """The data did not meet the gates. Carries the reasons."""

    def __init__(self, gate: Gate) -> None:
        super().__init__("; ".join(gate.reasons))
        self.gate = gate


@dataclass(frozen=True, slots=True)
class Result:
    version: str
    chosen: str
    threshold: float
    validation: dict[str, Evaluation]
    test: Evaluation
    baselines: Baselines
    split: Split
    gate: Gate
    manifest_path: Path


def train(
    dataset: Dataset,
    *,
    timezone: str,
    registry: Path,
    enforce_gates: bool = True,
) -> Result:
    """Build features, split, fit every candidate, choose on validation,
    score on test once, and write the registry entry.

    ``enforce_gates`` exists for the tests, which prove the mechanics on a
    few dozen hand-built rows. The command line has no such switch: no
    invented data ever reaches the registry through the front door.
    """
    frame = build(dataset, timezone)
    split = temporal_split(frame)
    verdict = gate(frame, split)
    if enforce_gates and not verdict.ok:
        raise GateRefusedError(verdict)

    validation: dict[str, Evaluation] = {}
    fitted: dict[str, Any] = {}
    thresholds: dict[str, float] = {}

    for name, factory in model_factories().items():
        model = factory()
        model.fit(frame.X[split.train], frame.y[split.train])
        scores = model.predict_proba(frame.X[split.validation])[:, 1]
        threshold = best_threshold(scores, frame.y[split.validation])
        validation[name] = evaluate(scores, frame.y[split.validation], threshold)
        fitted[name] = model
        thresholds[name] = threshold

    # Chosen on validation ROC-AUC: threshold-free, and it ranks models the
    # same way a later change of threshold would.
    chosen = max(validation, key=lambda n: validation[n].roc_auc)
    threshold = thresholds[chosen]
    test_scores = fitted[chosen].predict_proba(frame.X[split.test])[:, 1]
    test = evaluate(test_scores, frame.y[split.test], threshold)
    floor = baselines(frame.y[split.train], frame.y[split.test])

    version = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    entry = registry / version
    entry.mkdir(parents=True, exist_ok=False)
    joblib.dump(fitted[chosen], entry / "model.joblib")

    manifest = {
        "version": version,
        "trained_at": datetime.now(UTC).isoformat(),
        "question": f"interaction within the next {HORIZON_SECONDS // 60} minutes",
        "dataset": {
            "sha256": dataset.sha256,
            "events": len(dataset),
            "span_days": round(dataset.span_seconds / 86400, 2),
            "timezone": timezone,
        },
        "decision_points": len(frame),
        "positive_rate": round(frame.positive_rate, 4),
        "feature_names": list(frame.feature_names),
        "split": {
            "train": int(split.train.size),
            "validation": int(split.validation.size),
            "test": int(split.test.size),
            "embargoed": split.embargoed,
        },
        "gate": {"ok": verdict.ok, "reasons": list(verdict.reasons)},
        "validation": {name: asdict(e) for name, e in validation.items()},
        "chosen": chosen,
        "threshold": threshold,
        "test": asdict(test),
        "baselines": {"majority": asdict(floor.majority), "base_rate": asdict(floor.base_rate)},
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scikit_learn": sklearn.__version__,
            "random_state": RANDOM_STATE,
        },
    }
    manifest_path = entry / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    return Result(
        version=version,
        chosen=chosen,
        threshold=threshold,
        validation=validation,
        test=test,
        baselines=floor,
        split=split,
        gate=verdict,
        manifest_path=manifest_path,
    )


def _report(result: Result) -> str:
    def line(label: str, e: Evaluation) -> str:
        return (
            f"  {label:<22} P={e.precision:.2f} R={e.recall:.2f} F1={e.f1:.2f} "
            f"AUC={e.roc_auc:.2f}  tp={e.true_positives} fp={e.false_positives} "
            f"fn={e.false_negatives} tn={e.true_negatives}"
        )

    out = [f"registry entry {result.version}", "validation (model selection):"]
    out += [line(name, e) for name, e in result.validation.items()]
    out += [
        f"chosen: {result.chosen} at threshold {result.threshold:.3f}",
        "test (scored once):",
        line(result.chosen, result.test),
        line("baseline: majority", result.baselines.majority),
        line("baseline: base rate", result.baselines.base_rate),
        f"split: train={result.split.train.size} validation={result.split.validation.size} "
        f"test={result.split.test.size} embargoed={result.split.embargoed}",
        f"manifest: {result.manifest_path}",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from nova_ml.dataset import DatasetError, load

    parser = argparse.ArgumentParser(
        prog="python -m nova_ml.train",
        description="Train the interaction predictor on an exported dataset.",
    )
    parser.add_argument("--dataset", required=True, type=Path, help="CSV from export_dataset.py")
    parser.add_argument("--timezone", required=True, help="the user's IANA timezone")
    parser.add_argument(
        "--registry", type=Path, default=Path(__file__).resolve().parent.parent / "models"
    )
    args = parser.parse_args(argv)

    try:
        dataset = load(args.dataset)
    except (DatasetError, OSError) as error:
        print(f"cannot read dataset: {error}", file=sys.stderr)
        return 1

    try:
        result = train(dataset, timezone=args.timezone, registry=args.registry)
    except GateRefusedError as refused:
        print("not enough data to train, and a model on thin data would be a fabrication:")
        for reason in refused.gate.reasons:
            print(f"  - {reason}")
        return 2

    print(_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
