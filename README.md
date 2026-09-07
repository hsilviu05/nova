# NOVA

**Multimodal AI Companion, Physical Robot & Behavioral Intelligence Platform**

NOVA is a small 3D-printed creature that sits on a desk. It sees, hears,
speaks, moves, remembers, and reacts — and while it does, it produces a
structured record of its own behaviour. That record is the point.

Most AI-pet projects stop at "it talks". NOVA is also a **data collection
platform**: weeks of real telemetry from a real device, feeding a real
machine-learning pipeline that predicts when its owner will next interact
with it.

> **Status: Phases 1–8 of 10, unevenly.** The backend — API, persistence,
> auth, the device platform, conversational AI, semantic memory, analytics,
> and the ML pipeline — is built, tested, and running. The firmware's core is
> tested on a host; its device layer has never been compiled and nothing has
> run on hardware. The iOS client is written but has never been compiled. The
> ML pipeline has trained nothing, by rule: no synthetic data, and there is no
> device yet. See [Roadmap](#roadmap) for exactly what exists today.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [What is built today](#what-is-built-today)
- [Quick start](#quick-start)
- [The API](#the-api)
- [Authentication design](#authentication-design)
- [Hardware](#hardware)
- [Data and machine learning](#data-and-machine-learning)
- [Repository layout](#repository-layout)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Documentation](#documentation)

---

## Why this exists

A chatbot in a plastic shell is a demo. What makes a companion device
interesting is that it occupies a fixed point in someone's day and can
therefore *observe* — when they arrive, how long they work, when they go
quiet. That produces genuinely novel behavioural data, and behavioural data
supports genuine prediction.

So NOVA is built as three cooperating systems:

| System | Role |
|---|---|
| **Device** | An ESP32-S3 creature with an AMOLED face: microphone, speaker, servos, distance sensor, motion sensor. Senses and reacts, and keeps reacting when the network is gone. |
| **Backend** | Conversation, semantic memory, telemetry ingestion, analytics, and prediction. The device is thin; this is where the thinking lives. |
| **iOS app** | Chat, memory management, device control, and the analytics that make the collected data legible. Native SwiftUI, no third-party dependencies. |

The LLM never touches GPIO. It emits *intent* — `{"emotion": "curious",
"action": "look_at_user"}` — and a deterministic behaviour engine turns that
into servo positions. A language model that can command hardware directly is
a language model that can break hardware directly.

## Architecture

```
                    ┌──────────────────────┐
                    │      NOVA iOS        │
                    │   SwiftUI · Swift 6  │
                    │  chat · memory       │
                    │  insights · device   │
                    └──────────┬───────────┘
                               │ HTTPS / WebSocket
                    ┌──────────▼───────────┐
                    │     FASTAPI API      │
                    │  auth · devices      │
                    │  memory · analytics  │
                    │  ML · GitHub         │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
       PostgreSQL           Redis           AI layer
       + pgvector        rate limits    ┌──────┼──────┐
                          + cache       ▼      ▼      ▼
                                       LLM  Vision  STT/TTS
                                        └─────┼─────┘
                                       Behaviour engine
                                              │ WebSocket
                                  ┌───────────▼───────────┐
                                  │    NOVA ESP32-S3      │
                                  │  AMOLED face · touch  │
                                  │ mic · speaker · IMU   │
                                  │   servos · ToF        │
                                  └───────────────────────┘
```

Full detail, including the layering rules the backend follows, is in
[ARCHITECTURE.md](ARCHITECTURE.md).

## What is built today

Phases 1, 2 and 4 delivered a running, tested backend:

- **FastAPI** application with versioned `/api/v1` routes and OpenAPI docs
- **PostgreSQL 17 + pgvector**, async SQLAlchemy 2.0, Alembic migrations
- **Redis** for rate limiting
- **Authentication**: Argon2id password hashing, JWT access tokens, and
  database-backed refresh tokens with rotation *and reuse detection*
- **Observability**: structured JSON logs, a request ID on every log line and
  response, per-request latency
- **Health probes**: `/health` (liveness) and `/ready` (dependency readiness)
- **Device claim flow**: the device shows a code on its face, the owner types
  it into the app, and the device then collects a credential only it can
  collect
- **Versioned WebSocket protocol**: authenticated at the handshake, every
  frame validated against a discriminated union, heartbeat, telemetry
  ingestion, and owner-issued commands
- **Conversations**: provider-agnostic AI layer, replies streamed over
  Server-Sent Events, and a partial reply kept when the provider fails or the
  client disconnects
- **288 tests**, 96% branch coverage, `ruff` and `mypy --strict` clean

Phase 3 added the iOS client — sign-in, device claiming by typed code, a home
screen, and streaming chat — **written but never compiled**, see below.

`scripts/simulate_device.py` drives the entire device lifecycle against a
running API, so the flow is exercisable before the hardware arrives.

> **On the iOS app.** Everything else here was built and verified in a Linux
> environment. Swift cannot be: there is no toolchain for it there, and
> SwiftUI does not build on Linux at all. So the iOS code has never been
> through a compiler, and the first build will likely need fixes.
>
> What *is* verified is the part that would otherwise fail silently. Every
> model and test fixture was written against JSON captured from a running
> API, and `scripts/check_ios_contract.py` compares the Swift models against
> the live OpenAPI schema on every CI run — so a backend change that breaks
> the client fails the build rather than the app.

Everything above is verified running, not scaffolded. What is *not* built yet
is listed honestly in the [Roadmap](#roadmap).

## Quick start

Requirements: Docker and Docker Compose. Nothing else.

```bash
git clone <this-repo> nova && cd nova
cp .env.example .env
```

Generate the signing secret. The API refuses to start without it — there is
deliberately no default. The `tr` matters: `openssl` wraps base64 at 64
characters, and a value split across two lines breaks `.env` parsing.

```bash
echo "NOVA_JWT__SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')" >> .env
```

```bash
docker compose up --build
```

That starts Postgres, Redis, and the API; migrations run automatically before
the server binds. Then:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
open http://localhost:8000/docs
```

Running the API directly on your machine instead is covered in
[DEVELOPMENT.md](DEVELOPMENT.md).

## The API

Interactive documentation is at `/docs` (disabled in production).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. Touches no dependency. |
| `GET` | `/ready` | Readiness. Probes Postgres and Redis; 503 if either is down. |
| `POST` | `/api/v1/auth/register` | Create an account, return a token pair. |
| `POST` | `/api/v1/auth/login` | Exchange credentials for a token pair. |
| `POST` | `/api/v1/auth/refresh` | Rotate a refresh token. |
| `POST` | `/api/v1/auth/logout` | Revoke one session. |
| `POST` | `/api/v1/auth/logout-all` | Revoke every session for the caller. |
| `GET` | `/api/v1/users/me` | The authenticated user. |
| `PATCH` | `/api/v1/users/me` | Update the authenticated user. |
| `POST` | `/api/v1/devices/provision` | Device-facing. Start provisioning, return a claim code. |
| `POST` | `/api/v1/devices/provision/poll` | Device-facing. Collect credentials once claimed. |
| `POST` | `/api/v1/devices/claim` | Adopt the device showing a code. |
| `GET` | `/api/v1/devices` | List your devices. |
| `GET` | `/api/v1/devices/{id}` | Device detail, including live status. |
| `PATCH` | `/api/v1/devices/{id}` | Rename a device. |
| `DELETE` | `/api/v1/devices/{id}` | Remove a device and revoke its credentials. |
| `GET` | `/api/v1/devices/{id}/telemetry` | Recent telemetry. |
| `POST` | `/api/v1/devices/{id}/commands` | Send a command to a connected device. |
| `WS` | `/api/v1/devices/ws` | The device connection. |
| `GET` | `/api/v1/conversations` | List your conversations. |
| `POST` | `/api/v1/conversations` | Start a conversation. |
| `GET` | `/api/v1/conversations/{id}` | A conversation and its messages. |
| `DELETE` | `/api/v1/conversations/{id}` | Delete a conversation. |
| `POST` | `/api/v1/conversations/{id}/messages` | Send a message, wait for the reply. |
| `POST` | `/api/v1/conversations/{id}/stream` | Send a message, stream the reply (SSE). |
| `GET` | `/api/v1/memories` | What NOVA remembers about you. |
| `GET` | `/api/v1/memories/search` | Search memories by meaning, with scores. |
| `PATCH` | `/api/v1/memories/{id}` | Correct a memory. Editing the text re-embeds it. |
| `DELETE` | `/api/v1/memories/{id}` | Forget one thing. |
| `DELETE` | `/api/v1/memories` | Forget everything, without deleting the account. |
| `GET` | `/api/v1/devices/{id}/analytics` | Aggregated telemetry, bucketed in your local hours. |
| `GET` | `/api/v1/devices/{id}/insights` | What the telemetry supports saying — or why it doesn't. |

Every non-2xx response uses one envelope, always carrying the request ID that
appears in the server logs:

```json
{
  "error": {
    "code": "invalid_credentials",
    "message": "Incorrect email or password.",
    "request_id": "4fac82f6-44f2-4420-bd5c-c9283658c7d2",
    "details": {}
  }
}
```

## Authentication design

Two token types, chosen for different reasons.

**Access token** — a 15-minute JWT. Stateless, so verifying it costs no
database round trip. The algorithm is pinned at verification time; accepting
the token's own `alg` header is the classic JWT confusion vulnerability, and
there is a test that specifically forges an `alg: none` token.

**Refresh token** — a 384-bit opaque random string, stored only as a SHA-256
digest. Opaque because a refresh token must be *revocable*, and a stateless
JWT cannot be revoked before it expires. SHA-256 rather than Argon2 because
the input is full-entropy random data, so there is nothing for a slow KDF to
defend against.

Refresh tokens rotate on every use and are grouped into a **family** — every
token descended from one login. Presenting a token that was already rotated
means someone is holding a copy they should not:

```
login ──► token A
             │ refresh
             ▼
          token B          (A is now revoked)
             │
   attacker replays A  ──► entire family revoked, B dies too
```

Silent theft becomes a detected, contained event. Both the attacker and the
legitimate user are forced to re-authenticate, which is the correct outcome:
the alternative is an attacker refreshing quietly forever.

More in [SECURITY.md](SECURITY.md).

## Hardware

Roughly €100–118 all in. Full list with quantities and buying notes in
[`hardware/BOM.md`](hardware/BOM.md).

| Part | Choice | Why |
|---|---|---|
| Controller | Waveshare ESP32-S3-Touch-AMOLED-2.06 | 410×502 AMOLED touch face, **microphone, speaker, and ES8311 codec on board**, plus a 6-axis IMU and RTC. One board is the face, the ears, and the voice. |
| Movement | 3× SG90 micro servo | Head yaw and pitch. The third is because SG90 gears strip. |
| Servo driver | PCA9685 over I²C | **Required.** The controller reserves only I²C, UART, and USB pads — there is no free PWM GPIO. |
| Distance | VL53L0X time-of-flight | Presence, approach, and dwell. This is what emits `person_detected`. |
| Power | USB-C, plus a separate 5 V rail for servos | **No battery in V1.** Servos never draw from the board's regulator. |

**NOVA has no camera.** In this class of hardware a camera and a display are
mutually exclusive — both want the same pins — so the choice was sight or a
face. The face won: emotional presence is the product, an AMOLED's true black
makes drawn eyes read as a face rather than a screen, and ESP32-class vision
was always going to mean streaming frames to the backend at a few fps.

Crucially this costs nothing in the data-science story, because presence and
distance come from the time-of-flight sensor rather than from vision. The
reasoning, and what it would take to add a camera later, is in
[ADR 008](docs/decisions/008-amoled-face-hardware.md).

The body is ~12–15 cm, printed on a Bambu Lab printer. Not humanoid — a
small futuristic creature, original design. V1 does **not** walk: legged
locomotion costs most of the mechanical budget and buys the least. An
expressive face, voice, movement, and telemetry come first.

Design files land in [`hardware/`](hardware/) during Phase 6.

## Data and machine learning

The device emits structured telemetry:

```json
{
  "timestamp": "2026-09-06T19:32:14Z",
  "device_id": "nova-001",
  "event": "person_detected",
  "source": "time_of_flight",
  "distance_cm": 72,
  "head_rotation": 14,
  "emotion": "curious",
  "session_id": "abc123"
}
```

Weeks of that is a real dataset. The first ML problem is deliberately modest
and genuinely useful:

> **Will the user interact with NOVA in the next 10 minutes?**

Features come from time of day, day of week, time since last interaction,
recent interaction counts, session durations, proximity, and device state.
Models progress from logistic regression to random forest to gradient
boosting, evaluated on precision, recall, F1, ROC-AUC, and a confusion
matrix.

Splits are **temporal, never random**. Behavioural data is a time series;
shuffling it lets the model learn from its own future and produces a score
that means nothing. See [ADR 005](docs/decisions/005-ml-temporal-split.md).

**No synthetic data.** The dataset comes from the device or the ML phase does
not begin. Manufacturing a plausible CSV to demonstrate a pipeline proves
only that the pipeline runs.

## Repository layout

```
nova/
├── apps/ios/               Native SwiftUI client             (Phase 3)
├── services/api/           FastAPI backend                   ✅ Phase 1
├── firmware/nova-esp32/    ESP-IDF firmware, C++             (Phase 2/6)
├── ml/                     Dataset, features, training, registry ✅ Phase 8
├── hardware/               CAD, electronics, assembly        (Phase 6)
├── infrastructure/         Docker and deployment
├── docs/
│   ├── architecture/
│   ├── api/
│   ├── decisions/          Architecture Decision Records
│   └── hardware/
├── scripts/
├── docker-compose.yml
└── .env.example
```

## Testing

```bash
cd services/api
pytest                                    # 96 tests
pytest --cov=nova --cov-report=term-missing
ruff check . && ruff format --check .
mypy
```

The suite runs against **real PostgreSQL and Redis**, not in-memory fakes.
The things most likely to break — the unique index behind reuse detection,
`ON DELETE CASCADE`, partial indexes, `INET` columns — do not exist in
SQLite, so a SQLite suite would pass while production failed.

Failure paths are tested as deliberately as happy paths: wrong passwords,
unknown accounts, expired tokens, replayed tokens, deactivated users, forged
signatures, malformed payloads, a concurrent-registration race, and Redis
being unreachable.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| **1** | Backend foundation: API, Postgres, Redis, Docker, migrations, auth, logging | ✅ **Complete** |
| **2** | Device platform: claim flow, device auth, WebSocket protocol, telemetry | ✅ **Complete** |
| **3** | iOS foundation: SwiftUI app, auth, device claiming, home screen | ⚠️ **Written, not compiled** |
| **4** | AI chat: provider abstraction, conversations, streaming | ✅ **Backend complete** — Anthropic, local Ollama, or offline; iOS uncompiled |
| **5** | Semantic memory: extraction, embeddings, pgvector retrieval | ✅ **Backend complete**, iOS uncompiled |
| **6** | Physical robot: servos, animated AMOLED face, audio, proximity, IMU | ⏳ **Core, drivers, face and ESP-IDF layer written**; the core is tested, the device layer is uncompiled, and nothing has run on hardware |
| **7** | Telemetry and analytics: aggregation, insights screen | ✅ **Backend complete**, iOS uncompiled |
| **8** | Machine learning: dataset, features, temporal validation, predictions | ✅ **Pipeline complete and tested**; trains nothing until real telemetry exists |
| 9 | GitHub dev mode: webhooks, CI reactions | Planned |
| 10 | Production hardening: security review, performance, deployment | Planned |

Each phase ends with something that runs, not something that compiles.

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layering, request flow, schema conventions |
| [SECURITY.md](SECURITY.md) | Threat model, auth design, privacy posture |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Local setup, migrations, testing, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Commit conventions, code standards, review |
| [docs/decisions/](docs/decisions/) | Architecture Decision Records |

## License

MIT.
