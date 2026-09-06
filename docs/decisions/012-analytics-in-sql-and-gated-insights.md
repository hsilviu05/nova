# 012 — Aggregation in SQL, and insights that refuse to guess

**Status:** Accepted · **Date:** 2026-09-06

## Context

Phase 7 turns the telemetry Phase 2 ingests into something a person can read.
Three questions decided the shape of it, and the tempting answer was wrong
each time.

1. **Where does aggregation run?** The obvious answer is "load the rows and
   fold them in Python", which is easy to write and easy to read.
2. **In whose hours?** Telemetry timestamps are UTC. Everything on the screen
   is phrased as "you're usually around at...".
3. **When is NOVA allowed to say something?** The obvious answer is "whenever
   there is data", which is how a companion ends up announcing your daily
   routine after watching you for one afternoon.

## Decision

### Aggregate in SQL, bucket in the owner's timezone

Every query in `AnalyticsRepository` groups in Postgres, using
`recorded_at AT TIME ZONE :timezone`.

Two separate reasons, and the second is the important one:

**Volume.** A month of heartbeats at the default 30-second interval is around
ninety thousand rows for one device. Pulling those over the wire to count them
is slow and memory-hungry for no gain in clarity.

**Correctness.** Postgres resolves daylight saving from the tz database.
Adding a fixed offset in Python is wrong for half the year everywhere that
observes DST — and wrong in a way that looks completely fine, because the
numbers are still numbers.

Europe/Bucharest, 2026-03-29: 03:00 local does not happen.

| UTC | Local | Naive `+2h` |
|---|---|---|
| 00:30 | **02:30** | 02:30 ✓ |
| 01:30 | **04:30** | 03:30 ✗ — an hour that did not exist |

The autumn transition is the mirror: 2026-10-25 has 03:00 local twice, and
two events an hour apart in UTC belong in the *same* local bucket. Both cases
are tested against real Postgres.

This is why `User.timezone` stores an **IANA name, not an offset**. An offset
cannot express "Europe/Bucharest", only "what Bucharest happened to be doing
when the value was written".

### Weekday 0 is Monday, converted at the boundary

Postgres `EXTRACT(DOW)` counts Sunday as 0; Python's `date.weekday()` counts
Monday as 0. The repository converts, so nothing downstream has to remember
which convention it is holding. Returning the raw value would put every
weekday insight one day out, and the label would still read plausibly.

### No rollup table

At this data volume the index on `(device_id, recorded_at)` covers these
scans comfortably. A materialised rollup is a backfill, a refresh schedule,
and a staleness question, in exchange for nothing measurable today. When one
device holds millions of rows this decision should be revisited; it is not a
principle, it is a threshold that has not been crossed.

### Insights are gated on support, and say so when they decline

Every derived statement has a documented minimum it will not go below, and
returns nothing rather than guessing:

| Insight | Requires |
|---|---|
| Active hours | ≥ 3 distinct days **and** ≥ 20 events |
| Busiest weekday | ≥ 14 days, each weekday seen ≥ 2 times |
| Weekly trend | ≥ 15 events in **both** weeks, ≥ 20% change |
| Battery runtime | ≥ 8 readings over ≥ 45 min, R² ≥ 0.80, negative slope |
| Connectivity | ≥ 1 observed gap |

Three of these deserve their reasoning spelled out.

**Days, not events.** Two hundred detections from one afternoon is *one day*
of evidence. Gating on volume alone is exactly how a device announces a habit
it has never actually observed twice.

**Weekdays compared per occurrence.** A 17-day window contains three Mondays
and two Saturdays. Raw totals make Monday look busier on nothing but the
calendar. The comparison divides by how many times each weekday came round.

**R² decides whether a battery projection exists at all.** A slope fitted
through a curve is still a slope, and extrapolating one to "3 hours left"
turns a bad fit into a confident lie. Below 0.80 the readings are not
describing a trend and no projection is produced.

Every insight carries its `sample_size` and `days_observed` in the payload,
and the app shows both next to the headline. Somebody reading a claim about
their own life should be able to see what it rests on without tapping
anything.

### "Not enough data" is a response, not an error

`InsightsRead` returns an empty list plus an `insufficient_reason`. Three
distinct outcomes, deliberately distinguishable:

- **No telemetry at all** — "NOVA hasn't reported anything yet."
- **Below the coverage floor** — names how many days there are.
- **Enough to look at, nothing consistent** — "still watching".

An empty list *without* a reason would be indistinguishable from a bug, which
is why the server always sets one.

### Observed gaps, not an uptime percentage

Connectivity reports *silences between heartbeats*. A percentage needs a
denominator of time the device was **expected** to be reachable, and nothing
in the system knows that — a NOVA switched off overnight on purpose would
score identically to one that crashed. The insight says so in its own text.

## Consequences

**Good**

- Hour-of-day figures are correct across DST transitions, tested on both.
- Aggregation scales with the index rather than with the API's memory.
- NOVA cannot claim a pattern it has not actually observed.
- The statistics are pure functions with no database, so the thresholds are
  testable exhaustively and readable in one file.

**Bad**

- Every request is a scan. Mitigated by a hard 90-day cap on the window, but
  a device reporting for years will eventually need the rollup this decision
  declined to build.
- The thresholds are judgement, not calibration. They are defensible and
  documented, but nobody has measured how often a gated insight would have
  been correct. That needs real devices and real users.
- A quiet device gets no insights for weeks. That is intended, and it will
  still read as an empty screen to somebody who does not know why — which is
  what `insufficient_reason` exists to explain.
- Presence analytics rests entirely on `person_detected` from a single
  time-of-flight sensor. It measures "something is 60 cm away", not who, and
  a parcel left in front of NOVA is indistinguishable from a person.
