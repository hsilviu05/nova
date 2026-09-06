# NOVA API

FastAPI backend for NOVA: accounts, devices, conversations, semantic memory,
telemetry, analytics, and predictions.

See the repository root [README](../../README.md) for the system overview,
[ARCHITECTURE.md](../../ARCHITECTURE.md) for the layering, and
[DEVELOPMENT.md](../../DEVELOPMENT.md) for setup.

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn nova.main:create_app --factory --reload
```
