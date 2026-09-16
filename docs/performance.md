# Performance

What was measured, what was found, what was changed, and what the numbers
were before and after. Everything here was run on the development
container: four vCPUs, one uvicorn worker, PostgreSQL 16 and Redis on the
same box. Absolute numbers will be different on a real host; the ratios
are the point.

## Method

Two instruments, both in the repository:

- **`scripts/loadcheck.py`** — twenty simulated users at once, each
  registering, claiming a device, streaming telemetry over the real
  WebSocket, chatting through the offline provider, and pulling analytics.
  p50/p95/p99 per endpoint. It refuses to run unless the API reports a
  local or test environment, and every row it writes is marked synthetic.
- **`EXPLAIN (ANALYZE, BUFFERS)`** on every analytics query, with the real
  parameters, captured from the repository code by a SQLAlchemy cursor
  event rather than retyped. Run against 2.5 million telemetry rows: the
  seeded device carrying 400,000 spread over two years, plus 300 phantom
  devices with 7,000 each, so the planner faced the data shape a year of
  real use would produce.

A caveat about the second one, because it produced a wrong result first.
The initial bulk load put the seeded device's rows in one contiguous
block of time, and the planner — assuming device and time are
independent — estimated that 84% of the device's rows fell inside the
28‑day window and chose a plan that scanned all 400,000. That looked like
a missing index. It was a statistics artefact of the synthetic layout.
Re‑spreading the rows over two years produced the plans below, which are
the ones a real database would produce. Synthetic data for plan analysis
is fine; synthetic data with an unrealistic *shape* will send you fixing
the wrong thing.

## Findings

### 1. Password hashing blocked the event loop

Argon2 is deliberately slow — about 26 ms per hash at the configured cost —
and it was being called synchronously from the async auth service. Every
register or login held the worker's event loop for the whole hash, and
every other request on that worker waited behind it.

Measured with a probe that polls `/health` (which touches no dependency,
so its latency is the event loop's availability) every 10 ms while N
registrations are in flight:

| concurrent registrations | burst before | burst after | `/health` max during burst, before | after |
|---|---|---|---|---|
| 4 | 131–137 ms | 60–67 ms | 61–110 ms | 9.5–9.8 ms |
| 20 | 724 ms | 345 ms | 271 ms | 121 ms |

The fix is `asyncio.to_thread` around hash and verify. argon2‑cffi releases
the GIL, so the hashes genuinely run in parallel on separate cores: four
hashes serially take 105 ms and in four threads 41 ms. At twenty
concurrent registrations the four cores saturate and the loop thread is
competing for CPU rather than blocked — that is the remaining 121 ms, and
the cure for it is a bigger box or a second worker, not code.

The timing‑equalisation path (a failed login for an unknown email burns
the same CPU as a real verify, so the two are indistinguishable by
latency) moved with it.

### 2. A harmful index on `device_telemetry`

Every query against the table is scoped to one device. The standalone
index on `event_type` — a column with five distinct values, one of which
is 40% of the table — was being AND‑ed by the planner into the
device‑scoped aggregates, which then spent 13 of every 19 ms walking
half a million index entries to bitmap‑intersect them with 17,000 rows
the composite index had already found. The standalone `device_id` index
was merely redundant with `(device_id, recorded_at)`.

Both are replaced by `(device_id, event_type, recorded_at)`, which makes
the per‑type queries index‑only:

| query | before | after |
|---|---|---|
| `by_hour` | 19.5 ms | 2.0 ms |
| `by_day` | 19.7 ms | 1.9 ms |
| `by_weekday_hour` | 21.5 ms | 4.3 ms |
| `heartbeat_gaps` | 32.6 ms | 5.6 ms |
| `coverage` | 9.7 ms | 9.6 ms |
| `battery_series` | 2.0 ms | 2.2 ms |
| `event_type_counts` | 12.5 ms | 12.9 ms |

The last three were already on the right index and are unchanged. The
overview endpoint runs all of these in sequence on one connection, so its
SQL time on a device with 17,000 events in the window goes from about
117 ms to about 38 ms. The migration took four seconds on 2.5 million
rows. It holds a write lock while the index builds, which is fine at this
size; at a much larger one it would want `CREATE INDEX CONCURRENTLY`
outside a transaction.

### 3. Conversation detail loaded every message

`GET /conversations/{id}` returned the entire thread. A conversation that
has been open for months is thousands of rows, and this was the request
that got slower every day. It now returns the newest 200, oldest‑first,
through the same query the model's context window already used.
`message_count` still carries the total, so a client can tell when older
messages were left out. No schema change and no iOS change.

## Load check, before and after

Same twenty users, same ten rounds, same rate limits raised so one
machine can play twenty people. Latencies in milliseconds. The two runs
were made against the old and the new code from the same restart script,
after an earlier pair turned out to have both hit the old process — the
"after" server had failed to bind while the old one was still shutting
down, and the health check had cheerfully answered from the old one. The
table below is from runs where the process id was checked.

| endpoint | n | p50 before | p50 after | p95 before | p95 after |
|---|---|---|---|---|---|
| POST auth/register | 20 | 1213 | 1026 | 1755 | 1425 |
| GET users/me | 200 | 73 | 65 | 153 | 98 |
| POST conversations/{id}/messages | 200 | 243 | 249 | 353 | 320 |
| GET devices/{id}/analytics | 100 | 250 | 261 | 349 | 367 |
| GET devices/{id}/insights | 100 | 215 | 244 | 245 | 344 |
| GET devices/{id}/telemetry | 100 | 156 | 182 | 233 | 267 |
| GET conversations/{id} | 20 | 166 | 173 | 196 | 208 |
| GET memories | 100 | 106 | 115 | 128 | 134 |
| WS telemetry.batch (20 events) | 200 | 78 | 73 | 87 | 114 |

Throughput was 103 req/s before and 102 after, on one worker. The
register and `users/me` rows are the hashing fix. The rest is within the
run‑to‑run noise of a shared four‑core box that is also running Postgres
and the load generator — the analytics rows here are for fresh devices
with 200 events, where the index change is invisible; its effect is in
the table above. Nothing regressed outside that noise, which was the
thing to check.

## What was not done

- **No caching layer.** Nothing measured needed one. The insight engine
  recomputes from SQL on each request, and at 38 ms of SQL for a device
  with a month of dense telemetry that is cheaper than the invalidation
  logic a cache would need.
- **No connection‑pool tuning.** The defaults were never the bottleneck in
  any run.
- **No multi‑worker numbers.** The container has four cores shared with
  the database and the load generator; a two‑worker run here would
  measure contention, not the API. The production compose runs two.

## Reproducing

```bash
# Terminal 1: an API with limits raised, on a scratch database
NOVA_DATABASE__NAME=nova_load NOVA_AI__CHAT_PROVIDER=offline \
NOVA_SECURITY__AUTH_RATE_LIMIT_ATTEMPTS=100000 \
NOVA_DEVICE__PROVISION_RATE_LIMIT_ATTEMPTS=100000 \
NOVA_DEVICE__CLAIM_RATE_LIMIT_ATTEMPTS=100000 \
uvicorn nova.main:create_app --factory --port 8000

# Terminal 2
python scripts/loadcheck.py --users 20 --rounds 10
```

Delete the scratch database afterwards. Nothing the load check writes is
distinguishable from real telemetry except by the `synthetic` marker in
each row's payload.
