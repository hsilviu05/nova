# 013 — The ML pipeline's guarantees are code, not conventions

**Status:** Accepted · **Date:** 2026-09-07

## Context

Phase 8 asks one question — *will the user interact with NOVA in the next
ten minutes?* — and ADR 005 already settles the split: chronological, never
shuffled. This ADR records four further decisions made while building the
pipeline, each of which closes a way for a reported number to be better than
the truth.

The repository's rule stands: **no synthetic dataset.** There is no device
yet and therefore no data, so the pipeline was built and tested without ever
training a model that means anything. That constraint shaped everything
below.

## Decisions

### 1. Features cannot see forward, and a test proves it

Every feature at decision point `t` is computed by `searchsorted` against a
sorted time array using only events at or before `t`; every label uses only
events strictly after it. That is the shape of the code, not a keyword
argument.

It is also a test. `test_features.py::TestNoLookahead` computes features on
the full dataset and again on the dataset with everything after `t` deleted,
and asserts the rows are byte-identical. A mutation that lets a window peek
sixty seconds forward fails it; **no metric would have** — leakage does not
appear as a failure, it appears as a better score.

This is why pandas was not used. Its rolling windows can express "closed on
the left", but the guarantee is then a parameter somebody can drop. With
numpy the guarantee is the algorithm.

### 2. Decision points on a fixed grid, and the last horizon is dropped

Predicting at every telemetry row would hand a chatty device more samples
than a quiet one and make consecutive rows near-duplicates. Instead the span
is walked every five minutes and the question is asked once per point.

Rows in the final ten minutes of the observed span have no label: their
future was never observed. They are dropped, not labelled "no". Labelling
them "no" teaches the model that every dataset ends quietly, and it is a
mistake that is invisible until the model is asked about the present.

### 3. The split has an embargo

A row's label looks ten minutes into its future. The last training rows
before the validation boundary therefore carry labels computed from events
*inside* the validation period. `temporal_split` drops every row whose label
window crosses a boundary — ten minutes of rows per boundary, reported in
the manifest as `embargoed`. ADR 005 did not spell this out; it is the same
leak at the seam rather than in the shuffle.

### 4. Gates refuse to train, and the command line cannot override them

Below documented minimums — 1,500 decision points, 14 days of span, 60
positives, 15 in the test set, a 2% positive rate, both classes in every
split — `python -m nova_ml.train` exits with a named reason for each
shortfall and writes nothing. It does not produce a model with a caveat;
caveats get lost and numbers get quoted.

`train()` accepts `enforce_gates=False` so the tests can prove the mechanics
on a few dozen hand-built rows. The command line has no such flag. No
invented data reaches the registry through the front door, and a test pins
that the flag is rejected.

### 5. Selection on validation, test scored once, baselines beside it

Three candidates — logistic regression, random forest, gradient boosting —
are fitted on the training period. The threshold is chosen on validation by
F1; the model is chosen on validation by ROC-AUC; the winner is scored on
test exactly once. Two baselines are reported next to it: the majority
predictor, which never says "yes", and the base-rate predictor, which
guesses at the training positive rate. A model that does not clearly beat
both has learned nothing, whatever its accuracy says.

Seeds are fixed. Two runs on the same file produce the same manifest apart
from the timestamp, and a test checks that.

### 6. Provenance is a hash

The exporter (`nova.services.dataset_export`) writes a deterministic CSV —
sorted by time, then by source, with no clock-dependent field in the body —
and the registry manifest records its SHA-256 alongside the split sizes,
every model's validation score, the chosen model's test score, the
baselines, and the library versions. A number quoted from this pipeline can
be traced to the exact file and code that produced it.

The CSV contract is written down twice, on purpose: once in the pipeline,
once in the API, because the pipeline must not import the API. A test in the
pipeline reads the API's copy from source and compares the constants, so
drift fails in CI rather than in a training run.

## The line between fixtures and data

The tests build rows by hand — heartbeats every five minutes, a presence
event every twenty-five. Those rows exist to exercise labelling, windows,
the embargo, the gates and the registry. They are never trained on through
the front door, no number derived from them is reported, and the one test
that fits models on them discards every metric it computes. That is the
same line the analytics seeder drew: synthetic rows may prove the code; they
may never pretend to be observations.

## Consequences

- The pipeline is complete and tested, and has produced no model. That is
  correct. The first real registry entry will follow the first two weeks of
  device telemetry.
- Scores will be lower than a shuffled split, an unembargoed split, or a
  forward-peeking feature would report. Every one of those inflations has a
  test that fails if it is reintroduced.
- Time-series cross-validation (expanding windows) remains the upgrade once
  data allows, as ADR 005 says. The embargo carries over unchanged.
- Two dependencies, both BSD-3: numpy and scikit-learn (with joblib for
  persistence, declared because it is imported directly). No pandas, for the
  reason in decision 1.
