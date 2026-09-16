# Scripts

| Script | Purpose |
|---|---|
| `check.sh` | Runs lint, format, types, tests, and migration drift — everything CI runs. |
| `check_ios_contract.py` | Compares the Swift models against the API's OpenAPI schema. |
| `loadcheck.py` | Twenty simulated users against a running API; p50/p95/p99 per endpoint. Refuses to run outside a local or test environment. |

`check.sh` needs the API virtualenv active and a reachable PostgreSQL and
Redis (`docker compose up -d postgres redis`).

## check_ios_contract.py

The iOS app cannot be compiled without a macOS runner, so on every other
platform this is the one automated check between a backend schema change and a
client that silently decodes the wrong thing. It applies the same snake_case
conversion the decoder uses, then compares each Swift model's stored
properties with the schema it has to decode.

It also compares the enums the app mirrors from server literals — memory
categories and tool permissions. Those matter more than they look: a tool the
app believes is `read` when the server calls it `destructive` would show no
warning before somebody pressed the button.

```bash
python scripts/check_ios_contract.py                    # against a running API
python scripts/check_ios_contract.py openapi.json       # against a saved spec
```

Non-zero exit on a mismatch. It runs in CI against a schema exported without
starting a server, on Linux — so a contract break is caught on every push
rather than only where a macOS runner is available. The `ios` job builds and
tests the app itself; this job is what runs everywhere.

## loadcheck.py

Concurrency against a running API: each simulated user registers, chats
through the offline provider, runs a read-only tool and reads the dashboard;
the report is p50/p95/p99 per endpoint. It writes rows, so it refuses to run
unless `/health` reports a `local` or `test` environment.

Two numbers mean something different from the rest. `POST tools/invoke`
spawns a process, so it measures the machine rather than the API. `GET
system/status` probes the model provider and every configured project, so it
is the slowest endpoint by design and its number says more about what is
configured than about NOVA.

```bash
python scripts/loadcheck.py --users 20 --rounds 10
```

One machine playing twenty people trips the per-IP rate limits, which is
those limits working. For a load run, raise them on the API you are
testing; the script prints the variable names when it sees a 429. The
numbers it produced, and what was changed because of them, are in
[docs/performance.md](../docs/performance.md).
>>>>>>> origin/main
