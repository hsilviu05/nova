# NOVA

**A local-first personal AI terminal that remembers what you teach it and can
safely interact with your development environment.**

An old iPhone in a stand on the desk, a FastAPI backend on the Mac beside it,
and a model that never leaves the network. Ask it what is running, what broke,
what you worked on yesterday, and what it remembers about you — and let it
check, rather than guess.

```
                    ┌─────────────────────┐
                    │     OLD iPHONE      │
                    │                     │
                    │   NOVA (SwiftUI)    │
                    │                     │
                    │ Dashboard           │
                    │ Chat + voice        │
                    │ Memory              │
                    │ Tools               │
                    └──────────┬──────────┘
                               │
                      HTTP / SSE over Wi-Fi
                               │
                    ┌──────────▼──────────┐
                    │      NOVA API       │
                    │      FastAPI        │
                    │                     │
                    │ Conversations       │
                    │ Semantic memory     │
                    │ Tool orchestration  │
                    │ Audit log           │
                    └──────────┬──────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
       Ollama            PostgreSQL              Tools
    (or any local          + pgvector              │
     model server)                                 ├─ System
                                                   ├─ Docker
                                                   ├─ Git
                                                   ├─ GitHub
                                                   ├─ Projects
                                                   └─ Memory
```

The phone is a terminal, not a compute node. It authenticates, renders, and
listens; everything else happens on the machine under the desk.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Connecting the iPhone](#connecting-the-iphone)
- [The API](#the-api)
- [The tool system](#the-tool-system)
- [Memory](#memory)
- [Security](#security)
- [Repository layout](#repository-layout)
- [Testing](#testing)
- [What's next](#whats-next)
- [Documentation](#documentation)
- [History](#history)
- [License](#license)

---

## Why this exists

Everything a developer needs to know about their own machines is already
somewhere: in `docker ps`, in a health endpoint, in a git log, in a GitHub
tab, in their own head. The cost is not that the information is missing — it
is that answering "is SnapWorth up?" means finding a terminal, remembering the
command, and reading the output.

NOVA is a screen that is already on, that you can ask.

Three commitments shape every decision in this repository:

**Local-first.** The model runs on your machine through Ollama. Conversations,
memories and the audit log live in your own Postgres. Cloud providers are
supported and entirely optional; nothing degrades if you never configure one.

**Privacy-first.** Nothing leaves the network unless you configure something
that leaves the network. Speech recognition prefers the on-device recogniser.
Memories are listed, editable and deletable — there is no store of beliefs
about you that you cannot read.

**Safe by default.** NOVA can inspect your machine. Out of the box it can do
nothing else: every tool it ships with is read-only, Docker and shell
execution are switched off, and anything destructive needs you to say yes to
that specific action, in words, before it happens.

---

## What it does

Ask it things:

> "What's the status of SnapWorth?"
> "Is my API running?"
> "What containers are running?"
> "What did I work on yesterday?"
> "Show me my recent GitHub activity."
> "Search my memory for everything about RAG."
> "Explain the error from my latest deployment."
> "Remember that I prefer PostgreSQL for new projects."
> "Forget that."

A worked example, exactly as it happens:

```
You    Check SnapWorth.

NOVA   Checking SnapWorth…
       ⟳ project_health
       ✓ SnapWorth answered 200 (healthy). postgres: ok, redis: ok

NOVA   SnapWorth is healthy. The API answered in 8 ms, and both
       PostgreSQL and Redis are responding.
```

And when it wants to change something:

```
You    Get rid of the nova-test container.

NOVA   I can remove it, but that needs your confirmation.

       ┌──────────────────────────────────────────┐
       │ Remove the Docker container "nova-test"? │
       │ This cannot be undone.                   │
       │                                          │
       │   [ Yes, do it ]      [ Not now ]        │
       └──────────────────────────────────────────┘
```

NOVA cannot press that button. Nothing it says to itself, and nothing anything
it reads says to it, can press that button either.

---

## Architecture

| Layer | What it is | Why |
| --- | --- | --- |
| **iOS client** | SwiftUI, Swift 6 strict concurrency | Authenticates, renders, streams, listens. Holds no logic worth duplicating. |
| **API** | FastAPI, async SQLAlchemy 2.0 | Everything else. Runnable and testable with no phone in sight. |
| **Model** | Ollama by default | Local. Swappable behind one interface — see [ADR 002](docs/decisions/002-ai-provider-abstraction.md). |
| **Store** | PostgreSQL 17 + pgvector | Conversations, memories with embeddings, the audit log. |
| **Cache** | Redis | Rate-limit counters and confirmation tokens. Everything in it is disposable. |

The backend is layered strictly: **routes** own HTTP, **services** own
decisions, **repositories** own SQL, **models** own schema. A route that
queries, or a repository that decides, is a bug — see
[ARCHITECTURE.md](ARCHITECTURE.md) for why the boundaries are where they are.

---

## Quick start

Requires Docker, or Python 3.12+ with a PostgreSQL that has pgvector.

```bash
git clone https://github.com/hsilviu05/nova.git
cd nova

cp .env.example .env
echo "NOVA_JWT__SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')" >> .env

docker compose up --build
```

That brings up Postgres, Redis and the API on `http://localhost:8000`, with
migrations applied. Open `http://localhost:8000/docs`.

At this point NOVA works but has no model: the default provider is `offline`,
which answers with honest canned lines so the stack runs with nothing
installed. To give it a brain:

```bash
brew install ollama && ollama serve          # or your preferred install
ollama pull qwen3.8                          # any model with tool support

# in .env
NOVA_AI__CHAT_PROVIDER=ollama
NOVA_AI__CHAT_MODEL=qwen3.8:latest
NOVA_AI__OLLAMA_BASE_URL=http://host.docker.internal:11434
```

> **Pick a model that supports tools.** Ollama reports this — `ollama show
> <model>` lists `tools` under capabilities. A model without it will describe
> running a command instead of running one, which reads as NOVA claiming to
> have checked something it never looked at. NOVA asks the provider and stops
> offering tools when the answer is no, but it cannot make a model capable.

Then tell it what it is allowed to look at:

```bash
NOVA_TOOLS__WORKSPACE_ROOTS=["/Users/you/code"]
NOVA_INTEGRATIONS__PROJECTS=[{"name":"SnapWorth","base_url":"http://localhost:9000"}]
NOVA_TOOLS__DOCKER_ENABLED=true          # optional
NOVA_INTEGRATIONS__GITHUB_TOKEN=ghp_...  # optional, read-only scopes
NOVA_TOOLS__GITHUB_ENABLED=true
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for running the API on the host against
containerised dependencies, which is the faster loop.

---

## Connecting the iPhone

```bash
cd apps/ios
brew install xcodegen && xcodegen generate
open NOVA.xcodeproj
```

Build to the phone, then in the app: **Settings → NOVA server** and enter the
address of the Mac on your network — `http://192.168.1.20:8000`, or
`http://your-mac.local:8000`.

Four things worth knowing:

- **"Old" means 2018 or newer.** The app targets iOS 18, which runs on the
  iPhone XS, XR and everything since. iOS 17 supports exactly the same phones,
  so there is nothing to gain by lowering the target one version; going to
  iOS 16 would admit the iPhone 8 and X, at the cost of rewriting every
  `@Observable` model. Not done unless a phone that old is the one going in
  the stand.

- **`127.0.0.1` is the phone.** The default build address only works in the
  simulator. The Settings screen says so when it is still set.
- **Plain HTTP works on your own network and nowhere else.** The app declares
  `NSAllowsLocalNetworking`, which covers private and link-local addresses and
  `.local` names — and refuses to save an `http://` address outside them,
  rather than saving one that silently never connects.
- **iOS will ask for local network permission** the first time. Denying it
  means the app cannot reach your Mac at all.

Once signed in, Siri knows two phrases with no setup: *"Is SnapWorth up in
NOVA?"* and *"How is NOVA?"* Both read out the dashboard's verdict from the
lock screen, reach only the read-only status endpoint, and can change nothing.

For a phone living on a desk: **Settings → Display & Brightness → Auto-Lock →
Never**, and leave it on the charger. NOVA does nothing to defeat the lock
screen or keep itself running in the background; the dashboard polls while it
is on screen and stops when it is not.

---

## The API

`/api/v1`, bearer tokens, one error envelope everywhere.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/register` · `/auth/login` | Account and tokens |
| `POST` | `/auth/refresh` · `/auth/logout` · `/auth/logout-all` | Session lifecycle |
| `GET`/`PATCH` | `/users/me` | Profile |
| `GET`/`POST` | `/conversations` | List and start threads |
| `GET`/`DELETE` | `/conversations/{id}` | Read and remove one |
| `POST` | `/conversations/{id}/messages` | Send and wait (no tools) |
| `POST` | `/conversations/{id}/stream` | Send and stream (tools run here) |
| `GET` | `/memories` · `/memories/search` | What NOVA remembers |
| `PATCH`/`DELETE` | `/memories/{id}` | Correct or forget one |
| `DELETE` | `/memories` | Forget everything |
| `GET` | `/tools` | What NOVA can do here |
| `POST` | `/tools/invoke` | Run one, with confirmation if needed |
| `GET` | `/system/status` | The dashboard, in one response |
| `GET` | `/system/activity` | The audit log |
| `GET` | `/health` · `/ready` | Probes, outside the version prefix |

Every failure is the same shape:

```json
{
  "error": {
    "code": "tool_confirmation_required",
    "message": "Remove the Docker container “nova-test”? This cannot be undone.",
    "request_id": "9f2c…",
    "details": { "tool": "docker_remove_container", "confirmation_token": "…" }
  }
}
```

`code` is stable and machine-readable; `message` is for a person. Branch on the
first, show the second. `request_id` matches the `X-Request-ID` header and the
server logs, so a screenshot of an error is enough to find it.

Streaming is Server-Sent Events: `message`, `delta`, `tool`, `tool_result`,
`confirm`, `done`, `error`. Clients ignore event types they do not know, so the
server can add one without breaking an older build.

---

## The tool system

A tool is a name, a description, a JSON Schema, a permission, and an
`execute`. Everything NOVA can do to a machine goes through that interface,
because it is the only place a permission can be checked, an argument
validated, and an invocation recorded.

| Group | Tools | Default |
| --- | --- | --- |
| **System** | `system_health`, `system_resources`, `running_processes`, `disk_usage` | on |
| **Git** | `git_status`, `git_log`, `git_diff` | on |
| **Knowledge** | `memory_search`, `memory_create`, `memory_update`, `memory_delete` | on |
| **Projects** | `project_list`, `project_health` | on, if any are configured |
| **Docker** | `docker_status`, `docker_containers`, `docker_logs`, `docker_remove_container` | **off** |
| **GitHub** | `github_repositories`, `github_recent_commits`, `github_pull_requests`, `github_issues` | **off** |
| **Developer** | `execute_shell_command` | **off**, and allowlisted even then |

Three permission levels, and the line between the last two is where the
security model lives:

- **`read`** — no side effects. Runs whenever asked, including mid-reply.
- **`write`** — changes something *NOVA itself owns*, reversibly, visible in
  the app. In practice that means memory, and the registry refuses to
  register a `write` tool in any other group. Runs without confirmation,
  because NOVA already writes memories on its own from ordinary conversation.
- **`destructive`** — everything else. Always needs a person to approve that
  specific call, with those specific arguments.

The confirmation is a token: NOVA proposes, the server describes what would
happen and mints a token bound to the account, the tool and the arguments, and
only a request carrying that token runs anything. It is single use and expires
in three minutes. **The model never sees it.**

---

## Memory

NOVA forms beliefs about you from what you say, stores them as pgvector
embeddings, and retrieves them before it answers. It is the feature that makes
it worth talking to twice.

It is also the feature that would be unnerving without the rest of it: every
memory is listed in the app with its category, importance, confidence, and the
conversation it came from. Every one can be edited — which re-embeds it, so
retrieval follows the correction rather than the original — or deleted. "Forget
everything" is one button and does exactly that.

Extraction runs *after* a reply is delivered, never in the request path, and
refuses to store anything credential-shaped even if the model tries.

The default embedder is lexical: a hashed bag of word and character n-grams, so
cosine similarity measures **shared vocabulary**. It genuinely retrieves and
needs nothing installed. Its ceiling is real — it does not know "espresso"
relates to "coffee" — and the step up is a local embedding model through the
same Ollama that runs the chat:

```bash
ollama pull nomic-embed-text
# in .env: NOVA_AI__EMBEDDING_PROVIDER=openai_compatible
#          NOVA_AI__EMBEDDING_MODEL=nomic-embed-text
#          NOVA_AI__EMBEDDING_DIMENSIONS=768
cd services/api && python scripts/reembed_memories.py
```

The last line matters. Vectors from two embedders are not comparable, so a
switch leaves the old memories invisible to retrieval until they are
re-embedded — invisible rather than wrongly ranked, and counted as stale on
the dashboard until the pass runs. The pass is one transaction: a model
server that dies halfway leaves the store exactly as it was. See
[ADR 011](docs/decisions/011-lexical-embeddings-and-memory-extraction.md) and
[ADR 020](docs/decisions/020-embedding-width-is-a-ceiling.md).

---

## Security

NOVA has hands. [SECURITY.md](SECURITY.md) is the long version; the short one:

- Argon2id passwords, short-lived access JWTs, opaque rotating refresh tokens
  with reuse detection.
- Every tool call is audited — successes, failures, refusals, and whether the
  model or a person asked. Arguments are stored redacted.
- Tools never shell out through a shell. `execve` with an argv array, a
  stripped environment that contains no NOVA secret, a timeout, and bounded
  output.
- Filesystem tools are confined to configured roots, resolved through
  symlinks before the check.
- Tool output is treated as hostile input: secrets redacted, control sequences
  stripped, turn-marker impersonation defanged, and delivered inside a labelled
  block the output cannot close.
- The real bound is capability, not prompting. An injection that fully succeeds
  gets NOVA to run a different **read-only** tool, because that is all the chat
  path admits.

Found something? See [SECURITY.md](SECURITY.md#reporting-a-vulnerability).

---

## Repository layout

```
apps/ios/            SwiftUI client
  NOVA/App/            composition root, navigation
  NOVA/Core/           networking, SSE, Keychain, server settings
  NOVA/Features/       Dashboard, Chat, Memory, Tools, Voice, Settings
  NOVA/Models/         wire types, mirrored from the OpenAPI schema
  NOVATests/           decoding, contract and model tests

services/api/        FastAPI backend
  src/nova/ai/         provider interfaces and adapters
  src/nova/tools/      the tool system: base, registry, safety, adapters
  src/nova/api/        routes and dependency wiring
  src/nova/services/   business logic
  src/nova/repositories/  SQL
  src/nova/models/     ORM
  alembic/             migrations
  tests/               unit and integration

ml/                  dataset, features, split and training -- dormant, see below
deploy/              single-host production: Caddy, backups, release images
docs/decisions/      architecture decision records, including superseded ones
docs/                security review, performance numbers, deployment guide
scripts/             contract checks, load check, dependency auditing
```

---

## Testing

```bash
cd services/api
pytest                       # 443 tests, real Postgres and Redis
mypy                         # strict
ruff check . && ruff format --check .

cd ../../apps/ios
xcodebuild -scheme NOVA -destination 'platform=iOS Simulator,name=iPhone 17' test
```

Integration tests run against a real PostgreSQL and Redis rather than
in-memory fakes, because the things most likely to break — the unique index
behind reuse detection, `ON DELETE CASCADE`, partial indexes, pgvector
distance — do not exist in SQLite.

The suite needs no API key and no model server: the offline provider is a
first-class implementation, not a mock. The tests that exercise tool safety
run **real processes**, because a test that mocks `create_subprocess_exec`
proves the code calls a function, not that a branch named `; rm -rf ~` is
harmless.

`scripts/check_ios_contract.py` compares every Swift model against the
OpenAPI schema. The iOS app cannot be compiled without a macOS runner, so it
is the one automated check standing between a backend schema change and a
client that silently decodes the wrong thing.

---

## What's next

Deliberately not built yet, and each for a reason:

- **Local Whisper.** The `SpeechRecogniser` protocol exists precisely so this
  is a new file rather than a refactor.
- **Push notifications.** "Tell me when the deploy fails" needs a scheduler
  and an APNs certificate, which is a project of its own.
- **Multiple servers.** One address, one account, today.
- **A lock-screen widget.** Needs a widget extension, an App Group and
  Keychain sharing, which need a signing team in the project. The Siri
  intents are the half of that idea that works without one.

Explicitly **not** planned: autonomous agents that run unattended, unrestricted
shell access, multi-agent orchestration, Kubernetes, public deployment.

---

## Documentation

| Document | What is in it |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layering, the unit of work, error handling, the tool system |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Setup, migrations, testing, troubleshooting |
| [SECURITY.md](SECURITY.md) | Threat model, what is implemented, what is not |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Commits, branches, standards |
| [docs/security-review.md](docs/security-review.md) | Self-review findings; predates the tool system |
| [docs/performance.md](docs/performance.md) | What was measured, before and after |
| [docs/deployment.md](docs/deployment.md) | Single-host production: TLS, release images, backups |
| [docs/decisions/](docs/decisions/) | ADRs, including the ones this refactor superseded |

---

## History

NOVA began as a physical desk companion: an ESP32-S3 with an AMOLED face, two
servos and a distance sensor, and a backend built to provision it, speak its
WebSocket protocol, and do behavioural analytics on its telemetry.

That version is gone. The refactor to a phone-first terminal removed the
firmware, the hardware, the device tables and the telemetry pipeline, and kept
what turned out to be the actual product: the memory system, the conversation
engine, the provider abstraction, and the security work underneath all three.

The ADRs from that period are still in `docs/decisions/`, marked superseded.
They are kept because the reasoning still explains why several things are
shaped the way they are — the provider interface, the refresh-token rotation,
the pgvector schema — and because a decision log that quietly deletes the
decisions that did not last is not a log.

---

## License

MIT.
