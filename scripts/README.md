# Scripts

| Script | Purpose |
|---|---|
| `check.sh` | Runs lint, format, types, tests, and migration drift — everything CI runs. |
| `check_ios_contract.py` | Compares the Swift models against the API's OpenAPI schema. |

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
