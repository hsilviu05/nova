# Architecture

How NOVA is put together, and why.

## Principles

1. **The device is thin, the backend is thick.** An ESP32-S3 has no business
   running a language model. It senses, actuates, and reports; decisions
   happen server-side. The exception is offline behaviour, which is
   deliberately local (see [Offline behaviour](#offline-behaviour)).
2. **The LLM emits intent, never commands.** A model produces
   `{"emotion": "curious", "action": "look_at_user"}`. A deterministic
   behaviour engine converts that to servo angles. A model with direct GPIO
   access is a model that can drive a servo into its end stop.
3. **Business logic depends on interfaces, not vendors.** Swapping a speech
   provider must not touch conversation code.
4. **Every layer has one job.** Routes translate HTTP. Services decide.
   Repositories query. Nothing does two of those.

## Backend layering

```
    HTTP request
         │
         ▼
┌──────────────────┐  Middleware
│ request context  │  request ID, access log, latency
│ error handling   │  every failure → one JSON envelope
└────────┬─────────┘
         ▼
┌──────────────────┐  API layer          nova/api/
│ route handlers   │  parse, call one service, return
└────────┬─────────┘  no queries, no rules
         ▼
┌──────────────────┐  Service layer      nova/services/
│ business logic   │  every decision lives here
└────────┬─────────┘  raises domain errors, not HTTPException
         ▼
┌──────────────────┐  Repository layer   nova/repositories/
│ data access      │  all SQL, no rules
└────────┬─────────┘
         ▼
┌──────────────────┐  Persistence        nova/models/, nova/db/
│ ORM + engine     │
└──────────────────┘
```

The dependency arrow only points down. A repository never calls a service; a
service never builds an HTTP response.

### Why the boundaries are where they are

**Routes are thin** because HTTP is one of several transports NOVA will
speak. Phase 2 adds a WebSocket protocol for the device, and Phase 4 adds
streaming. Logic that lives in a route handler has to be duplicated or moved
the moment a second transport needs it.

**Services raise domain errors** (`ConflictError`, `AuthenticationError`)
rather than `HTTPException`. A service is then callable from a WebSocket
handler, a background job, or a test, without dragging FastAPI along. One
exception handler maps the domain hierarchy onto status codes in a single
place.

**Repositories own SQL** so query changes have one home, and so services can
be tested against a fake repository when a real database would add nothing.

## Dependency injection

Objects have two lifetimes.

**Application-scoped**, built once during the lifespan and parked on
`app.state`: the engine, the session factory, the Redis client, the token
service, the password hasher. These hold connections or expensive
configuration; rebuilding them per request would be wasteful and would exhaust
the pool.

**Request-scoped**, built by `nova/api/deps.py`: the session, repositories,
and services. Each request gets its own unit of work.

`create_app(settings)` takes its settings as an argument and nothing is
constructed at import time. That is what lets the test suite build an app
against a test database without the module having already connected to
something — and it is why the server is served through uvicorn's
`--factory` mode.

## The unit of work

One transaction per request. `session_scope` yields a session, commits if the
handler returns, and rolls back if it raises. Handlers never call `commit()`,
so a handler that fails halfway cannot leave a partial write.

There is exactly one deliberate exception. When refresh-token reuse is
detected, the service revokes the token family and then **commits before
raising** the 401. Without that explicit commit the rollback triggered by the
error would undo the revocation, leaving the compromised family alive and
reducing reuse detection to a log line with no effect. It is commented as such
at the call site, and a test fails if the commit is removed.

## Configuration

All configuration comes from the environment, through `pydantic-settings`,
with the `NOVA_` prefix and `__` nesting: `NOVA_DATABASE__HOST` populates
`Settings.database.host`. Values are typed and validated at startup, so a
malformed port fails immediately rather than at first use.

Secrets have **no defaults**. A missing `NOVA_JWT__SECRET_KEY` is a startup
failure, and a key shorter than 32 characters is rejected. A development
default that silently reaches production is worse than a loud crash.

## Error handling

Every failure produces the same envelope:

```json
{"error": {"code": "...", "message": "...", "request_id": "...", "details": {}}}
```

`code` is stable and machine-readable; clients branch on it, not on prose.
`request_id` matches the `X-Request-ID` response header and every server log
line for that request, so a user-reported failure is directly greppable.

Unhandled exceptions log their detail and return none of it. Internal
messages disclose schema names, file paths, and occasionally credentials.

Validation errors are reduced to `type`, `loc`, and `msg`. Pydantic's `input`
field is deliberately dropped: on a failing password rule it would reflect the
submitted password back to the caller and into any log or proxy that records
response bodies.

## Observability

`structlog` renders JSON in deployed environments and colourised console
output locally. A middleware assigns each request a UUID and binds it to a
context variable, so every log line emitted while handling that request
carries it without any logger being threaded through call signatures.

Incoming `X-Request-ID` headers are **not** trusted by default. Accepting one
would let a caller forge correlation between unrelated requests, or inject
arbitrary text into log fields. When NOVA runs behind a gateway that sets the
header itself, `trust_incoming_id=True` enables it — and even then only a
well-formed UUID is accepted.

Probes are split by purpose. `/health` answers without touching any
dependency, so a database outage cannot make an orchestrator kill an otherwise
healthy container. `/ready` probes Postgres and Redis and returns 503 if
either is unreachable, so a load balancer stops routing to an instance that
cannot serve.

## Database conventions

- **UUID primary keys**, so the firmware and the mobile app can mint
  identifiers offline without coordinating with the database.
- **Timezone-aware UTC** everywhere. `nova.core.clock.utc_now` is the single
  time source, which also gives tests one seam to freeze.
- **Explicit constraint naming**, so Alembic emits stable, diffable names
  instead of database-generated ones.
- **`ON DELETE CASCADE`** from user to owned rows: deleting an account must
  actually delete the account's data.
- **Partial indexes** where a query only ever touches live rows — active
  refresh tokens, for instance, stay a small index as token history grows.

Migrations are Alembic, checked into version control, and run automatically
before the API binds. `alembic check` runs in CI so a model change without a
migration fails the build rather than drifting silently.

`pgvector` is enabled in the very first migration even though nothing uses it
until Phase 5. Creating an extension is a database-wide privileged operation;
doing it up front means the migration that adds an embedding column is an
ordinary column addition.

## Redis

Redis holds rate-limit counters and cache entries — things that are cheap to
rebuild. It is not a database and holds nothing NOVA cannot lose.

That informs the failure mode: the rate limiter **fails open**. A Redis
outage should degrade abuse protection, not take login down. It logs a warning
and allows the request.

## AI provider abstraction

```
        Conversation service
                 │  depends on interfaces only
    ┌────────────┼────────────┬─────────────┐
    ▼            ▼            ▼             ▼
ChatProvider VisionProvider Embedding   SpeechProvider
    │            │          Provider         │
    └────────────┴────────────┴──────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   Cloud vendor A  Cloud vendor B  Local model
```

Configuration selects the implementation. No business logic imports a vendor
SDK. This is not speculative generality: speech and vision pricing and quality
move fast, and running a local model on a home server is a realistic goal for
this project. See [ADR 002](docs/decisions/002-ai-provider-abstraction.md).

## Semantic memory

```
  Exchange ──▶ extraction ──▶ scoring ──▶ embedding ──▶ pgvector
   (after                                                  │
  the reply)                                               │
                                                           ▼
  Next message ──▶ embed query ──▶ nearest, owner-scoped ──┘
                                        │
                                        ▼
                          second system block, after the
                          persona's cache breakpoint
```

Extraction runs **after** the response is delivered, as a background task
with its own database session. It is a second model call, and nobody should
wait on NOVA deciding what to remember.

Retrieval happens before the reply, scoped to the owner **in the WHERE
clause**. That is a correctness boundary rather than a filter: the retrieved
text goes straight into a prompt, so a leak there is a leak into somebody
else's conversation.

Embeddings come from `EmbeddingProvider`, not from the chat vendor —
Anthropic has no embeddings endpoint at all, which is exactly the case ADR
002 separated the interfaces for. The default `LexicalEmbeddingProvider`
measures shared vocabulary and genuinely retrieves; it does not know that
"espresso" relates to "coffee". Retrieval quality is capped there until a
real embedder is configured. See
[ADR 011](docs/decisions/011-lexical-embeddings-and-memory-extraction.md).

## Analytics and insights

```
  device_telemetry ──▶ aggregate in SQL ──▶ charts
   (UTC instants)      AT TIME ZONE tz          │
                                                ▼
                                      insight engine (pure)
                                                │
                              ┌─────────────────┴──────────────┐
                              ▼                                ▼
                    enough support?                    not enough?
                    statement + evidence          insufficient_reason
```

Bucketing happens in **Postgres**, in the owner's IANA timezone. Adding a
fixed offset in Python is wrong for half the year everywhere that observes
daylight saving, and wrong in a way that still looks like a number. Both
transitions are tested against a real database.

The insight engine is pure functions taking plain values — no ORM, no
session — so its thresholds are exhaustively testable and readable in one
file. Every statement it produces carries the sample size and the number of
distinct days behind it, and every one has a minimum below which it returns
nothing at all.

**Volume is not evidence.** Two hundred detections in a single afternoon is
one day of observation, and produces no claim about anybody's habits. "Not
enough data yet" is a normal response with its own reason string, not an
error and not an empty list. See
[ADR 012](docs/decisions/012-analytics-in-sql-and-gated-insights.md).

## Device protocol (Phase 2)

Versioned, strictly validated JSON over WebSocket. Malformed messages are
rejected, not tolerated.

```json
{
  "type": "robot.command",
  "version": 1,
  "command": "head.move",
  "payload": {"yaw": 20, "pitch": -5, "duration_ms": 500}
}
```

WebSocket rather than MQTT: NOVA has one device class talking to one backend
over a connection that is already authenticated and already carries
request/response traffic. MQTT adds a broker to operate and secure, in
exchange for a fan-out topology NOVA does not have. See
[ADR 004](docs/decisions/004-websocket-device-protocol.md).

## Offline behaviour

The firmware must stay alive when the backend is not. On disconnect the device
enters `OFFLINE` and runs a local state machine: idle eye animation, proximity
reactions, touch and motion responses. A companion that freezes when the WiFi
drops is a companion that feels broken. The onboard RTC keeps time-aware
behaviour working without a backend round trip.

State machine: `IDLE → LISTENING → THINKING → SPEAKING`, plus `CURIOUS`,
`HAPPY`, `CONFUSED`, `ALERT`, `SLEEPING`, `OFFLINE`. Each state drives the
animated face, head position, and audio together.

## Privacy

The microphone is the most sensitive component in the system, so the defaults
are conservative:

- Raw audio is **never stored by default**. Storage is opt-in.
- Telemetry records structured events (`person_detected`, distance,
  interaction start and end), not the audio that produced them.
- Memory is user-visible, user-editable, and user-deletable, with a
  separate "forget everything" that does not delete the account —
  wanting NOVA to stop knowing things about you is a different
  intention from wanting to stop using it.
- Extraction is instructed to skip credentials and identifiers, and a
  pattern check drops anything that looks like a secret regardless.
- Account deletion cascades to every owned row.

NOVA has no camera ([ADR 008](docs/decisions/008-amoled-face-hardware.md)),
which removes the most invasive sensor a desk device can carry. Presence
detection is a time-of-flight distance reading — it can tell that something is
72 cm away, and nothing about who or what it is. That is a meaningful privacy
property, not merely a consequence of the hardware choice, and it should
survive any later decision to add a camera.
