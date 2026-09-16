# NOVA API

FastAPI backend for NOVA: accounts, conversations, streaming replies, semantic
memory, a permissioned tool system, and the audit log of everything it ran.

This is the whole of NOVA apart from its screen. It runs, and is fully
testable, with no phone connected to it.

See the repository root [README](../../README.md) for the system overview,
[ARCHITECTURE.md](../../ARCHITECTURE.md) for the layering,
[DEVELOPMENT.md](../../DEVELOPMENT.md) for setup, and
[SECURITY.md](../../SECURITY.md) before enabling any capability that reaches
outside this process.

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn nova.main:create_app --factory --reload
```

It starts with no model attached and no capabilities beyond reading this
machine. That is the intended first run: `GET /api/v1/system/status` says
plainly what is missing.
