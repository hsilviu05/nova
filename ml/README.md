# NOVA Machine Learning

**Phase 8.** Nothing here runs until the device has collected real telemetry.

## The problem

Will the user interact with NOVA in the next 10 minutes?

## Pipeline

```
telemetry → validation → cleaning → feature engineering
         → temporal split → training → evaluation
         → model registry → inference → prediction API
```

Reproducible from the command line, not from notebooks:

```bash
python -m ml.training.train
```

`notebooks/` is for exploration only. Nothing in the production path imports
from it.

## Method

Baseline logistic regression, then random forest, then gradient boosting.
Evaluated on precision, recall, F1, ROC-AUC, and a confusion matrix — never
accuracy alone, which a constant "no" predictor would win.

Splits are chronological. See
[ADR 005](../docs/decisions/005-ml-temporal-split.md) for why random splits
are wrong for this data.

**No synthetic data.** Collect first, then analyse, then model.
