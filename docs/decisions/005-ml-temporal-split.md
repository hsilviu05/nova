# 005 — Temporal splits for behavioural machine learning

**Status:** Accepted · **Date:** 2026-09-06

## Context

The first ML problem is: *will the user interact with NOVA in the next 10
minutes?* Features include hour of day, day of week, time since last
interaction, interaction counts over recent windows, average session duration,
proximity, and device state.

This is a time series of one person's behaviour. Consecutive rows are heavily
correlated, and several features are explicit aggregates over recent history.

## Decision

**Split train, validation, and test chronologically.** Train on the earliest
period, validate on the middle, test on the most recent. Never shuffle.

Feature engineering computes every window using only data available at that
row's timestamp. Any aggregate that could see forward is a bug, not a
tuning choice.

The evaluation reports precision, recall, F1, ROC-AUC, and a confusion matrix
— not accuracy alone. If the user interacts in 10% of windows, a model that
always predicts "no" scores 90% accuracy and is worthless. Recall and
precision are what say whether the prediction is usable.

Every trained model is stored with its version, training date, feature set,
and evaluation metrics, so a reported number can always be traced to the data
and code that produced it.

## Alternatives considered

**Random train/test split.** The scikit-learn default, and wrong here. With
autocorrelated behavioural data it puts adjacent minutes on both sides of the
split, so the model is effectively tested on data it has already seen. The
resulting score is inflated and does not survive contact with real use. This
is the single most common methodological error in behavioural ML, and the
reason this ADR exists.

**K-fold cross-validation.** Same defect, repeated k times. Standard k-fold
trains on future data to predict the past.

**Time-series cross-validation (expanding-window folds).** Methodologically
sound and a genuine improvement in variance of the estimate. Deferred rather
than rejected: it needs more data than the first weeks of collection will
produce. A simple chronological split is the honest choice while the dataset
is small, and this is the natural upgrade once it is not.

**Group split by day.** Reasonable, and it removes within-day leakage.
Rejected as insufficient on its own: it still permits training on a later week
to predict an earlier one, which is the same leak at coarser granularity.

## Consequences

- Reported metrics reflect performance on genuinely unseen future behaviour.
- Scores will look *worse* than a shuffled split would report. That is the
  point; the shuffled number was never real.
- The test set is a single contiguous period, so it may be unrepresentative
  (a holiday week, an unusual work pattern). Documented as a limitation and a
  reason to move to expanding-window folds once data allows.
- Feature engineering must be written carefully, since leakage is easy to
  introduce and invisible in the metrics — it makes them better. Feature tests
  assert that windows never reach past their row's timestamp.
- The models cannot be trained until real data exists. That is deliberate:
  **no synthetic dataset.** Fabricating data to demonstrate a pipeline proves
  only that the pipeline runs, and it is the exact thing an interviewer probes.
