# 016 — Production posture: refuse to boot, one host, nothing published but the edge

**Status:** Accepted · **Date:** 2026-09-08

## Context

Phase 10 is "production hardening: security review, performance,
deployment". Each of those produced a decision worth recording, because
each had a cheaper alternative that would have looked the same on the
day it shipped and different six months later.

## Decisions

### Unsafe production settings are a boot failure, not a warning

The settings layer refuses to construct in production with debug on, a
CORS or Host wildcard, a non-HTTPS public URL, SQL echo, JSON logging
off, or the JWT secret every CI run uses. It names every problem in one
error.

The alternative was to log a warning for each. Warnings at startup are
read by nobody: the process starts, the health check goes green, the
deployment is declared done, and the log line scrolls past. A refused
boot is the only failure loud enough to be noticed before the first
request. The cost is that a legitimate exception needs a code change,
which is the intended friction.

### The body cap sends its own response

The first version raised an exception from the ASGI `receive` callable
and let the exception handlers render it. That produced a 400 from
FastAPI's own body parser on every route with a request model, because
FastAPI catches everything around its body read. The middleware now
sends the 413 itself and discards whatever the application says
afterwards.

The alternative was to check `Content-Length` eagerly and refuse before
the application runs, which is simpler and handles the declared case.
It does nothing about a chunked body, and a client that wants to exhaust
a worker does not have to declare a length. The lazy check also means a
route that never reads its body is not refused for a header alone.

### Argon2 runs in a thread, not a process pool, and is not made cheaper

Password hashing moved off the event loop with `asyncio.to_thread`.
argon2-cffi releases the GIL, so the threads genuinely parallelise.

Two alternatives were rejected. Lowering the Argon2 cost would have made
the numbers look better and the hashes weaker; the cost is the OWASP
minimum and stays there. A process pool would have avoided any doubt
about the GIL at the price of a second process per worker and a pickling
boundary for every login, for a difference that could not be measured
once the GIL release was confirmed.

### One composite index, and the single-column ones go

`(device_id, event_type, recorded_at)` replaces the standalone indexes on
`device_id` and `event_type`. Adding the composite without dropping the
others was the obvious move and the wrong one: the `event_type` index was
not merely unused but actively chosen by the planner, and it was chosen
because it existed. Every index is also a write cost on a table that
takes every telemetry event the device sends.

### One host, one compose file, nothing published but Caddy

The production stack is a single `docker-compose.prod.yml`: Caddy for
TLS, the API, Postgres, Redis, a backup sidecar. Only Caddy publishes a
port. uvicorn trusts forwarded headers from any peer, which is safe
precisely because there is no port to spoof through.

The alternatives were a managed platform, which would have hidden the
proxy-header and migration questions rather than answered them, and
Kubernetes, which for one API and one database is a way of having more
YAML than code. The single-host assumptions — migrations on start, the
in-memory connection registry, one Redis — are each written down where
they live so that outgrowing them is a known list, not an archaeology.

### The image is built from a tag, and never called `latest`

The release workflow builds on a version tag and publishes `v0.1.0`,
`v0.1` and the commit SHA. There is no `latest`. A deployment that says
`latest` says nothing about what is running, and a rollback to it is a
rollback to whatever happened to be built last.

## Consequences

- A wrong `.env` fails in seconds with a list of reasons. Nobody has to
  know the list in advance.
- The body cap is enforced on every route, including the ones added
  later by someone who has never heard of it.
- Login costs the same CPU it did; it just no longer costs everyone
  else's request the same wait.
- The analytics queries stay index-only as the table grows. If a new
  aggregate filters on a column the composite does not lead with, it
  gets its own measured decision, not a reflexive `index=True`.
- The production stack has not been run on a public host. The first
  deployment will find things; the design is meant to make what it finds
  small and named.
