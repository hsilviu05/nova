# NOVA Machine Learning

**Phase 8.** The pipeline is built and tested. It has trained nothing, and
will not until the device has collected real telemetry.

## The question

> Will the user interact with NOVA in the next 10 minutes?

An interaction is the time-of-flight sensor noticing somebody
(`person_detected`) or a message to NOVA from the phone. Heartbeats and
state changes are context, not interactions.

## Running it

```bash
# 1. Export a device's history from the API's database.
cd services/api
python scripts/export_dataset.py --device-id <uuid>      # -> ml/data/<uuid>.csv

# 2. Train.
cd ../../ml
pip install -e ".[dev]"
python -m nova_ml.train --dataset data/<uuid>.csv --timezone Europe/Bucharest
```

On thin data the second step prints why and exits 2:

```
not enough data to train, and a model on thin data would be a fabrication:
  - 96 decision points, need at least 1500
  - data spans 0.3 days, need at least 14
  - 19 positive examples, need at least 60
```

There is no flag to override that. When it does train, it writes
`models/<version>/model.joblib` and a `manifest.json` recording the dataset's
SHA-256, the split, every candidate's validation score, the chosen model's
test score, two baselines, and the library versions.

## What the tests are actually about

Read `tests/` as a list of ways a behavioural score gets inflated:

- a feature window that reaches sixty seconds past its row
- an interaction at the decision point counted as the future
- the last ten minutes of the data labelled "quiet" because nothing was seen
- training rows whose labels were computed inside the validation period
- a model chosen, or a threshold picked, on the test set
- a metric reported with no baseline beside it

Each is a case, and each was verified by breaking the guard and watching the
right test fail — ten mutations, ten caught. The one that matters most is
the no-lookahead test, because leakage is the one bug that makes the metrics
*better*. See [ADR 013](../docs/decisions/013-ml-pipeline-guarantees.md).

## Layout

```
nova_ml/
├── dataset.py    the CSV contract; what counts as an interaction
├── features.py   labels and features, from the past only
├── split.py      chronological split with an embargo; the gates
└── train.py      three models, evaluation, the registry, the CLI
tests/            hand-built rows that prove the code and are never data
data/             exports land here (gitignored)
models/           the registry (gitignored)
notebooks/        exploration only; nothing imports from it
```

## Method

Baseline logistic regression, then random forest, then gradient boosting.
Evaluated on precision, recall, F1, ROC-AUC and a confusion matrix — never
accuracy alone, which a constant "no" predictor would win.

Splits are chronological with an embargo at each boundary. See
[ADR 005](../docs/decisions/005-ml-temporal-split.md) for why random splits
are wrong for this data and [ADR 013](../docs/decisions/013-ml-pipeline-guarantees.md)
for the rest.

**No synthetic data.** Collect first, then analyse, then model.
