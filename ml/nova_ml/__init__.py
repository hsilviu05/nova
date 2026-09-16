"""NOVA behavioural ML.

The question is narrow on purpose: *will the user interact with NOVA in the
next ten minutes?* Everything here exists to answer it without lying --
features that cannot see forward, a split that cannot leak, gates that refuse
thin data, and an evaluation a constant predictor cannot win.

Nothing here runs on invented data. The tests build a handful of rows by hand
to prove the code; those rows are never trained on and never reported.
"""

HORIZON_SECONDS = 10 * 60
"""The prediction horizon. The label for a row is whether an interaction
happened in the (t, t + HORIZON_SECONDS] window after it."""
