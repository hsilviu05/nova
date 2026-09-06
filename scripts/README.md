# Scripts

| Script | Purpose |
|---|---|
| `check.sh` | Runs lint, format, types, tests, and migration drift — everything CI runs. |

`check.sh` needs the API virtualenv active and a reachable PostgreSQL and
Redis (`docker compose up -d postgres redis`).
