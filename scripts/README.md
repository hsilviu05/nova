# Scripts

| Script | Purpose |
|---|---|
| `check.sh` | Runs lint, format, types, tests, and migration drift — everything CI runs. |
| `simulate_device.py` | Drives the whole device lifecycle against a running API, standing in for hardware that has not arrived yet. |

`check.sh` needs the API virtualenv active and a reachable PostgreSQL and
Redis (`docker compose up -d postgres redis`).

## simulate_device.py

A fake NOVA. It provisions, displays a claim code, waits to be claimed, opens
a WebSocket, streams telemetry, receives a command, and gets factory-reset —
so the whole Phase 2 flow can be exercised without the board.

```bash
docker compose up -d          # or run the API on the host
python scripts/simulate_device.py
```

It asserts as it goes, so a non-zero exit means something regressed. Among
what it checks: the claim response never carries the device token, a spent
claim code stops working, a credential is collected exactly once, out-of-range
servo commands are rejected, another user gets 404 on every device route, and
a factory reset revokes the old credential.

Requires `httpx` and `websockets` (both in the API's dev extras).
