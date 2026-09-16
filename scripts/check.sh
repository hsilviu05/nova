#!/usr/bin/env bash
# Run every check CI runs, in the same order. Faster than pushing and waiting.
set -euo pipefail

cd "$(dirname "$0")/../services/api"

echo "── declared dependencies ────────────────────────"
# First, because an undeclared dependency is invisible to every check that
# runs inside a virtualenv that already has it -- and breaks every
# environment that installs from the project's own metadata.
python scripts/check_declared_dependencies.py

echo "── lint ─────────────────────────────────────────"
ruff check .

echo "── format ───────────────────────────────────────"
ruff format --check .

echo "── types ────────────────────────────────────────"
mypy

echo "── tests ────────────────────────────────────────"
pytest --cov=nova --cov-report=term-missing

echo "── migrations match models ──────────────────────"
alembic check

echo "── iOS contract ─────────────────────────────────"
# The Swift app cannot be compiled here, so this is what stands between a
# schema change and a client that silently decodes the wrong thing. The
# schema is exported from the app factory without binding a port.
python - <<'PYEOF'
import json

from nova.core.config import JWTSettings, Settings
from nova.main import create_app

settings = Settings(environment="test", jwt=JWTSettings(secret_key="x" * 40))
with open("openapi.json", "w") as handle:
    json.dump(create_app(settings).openapi(), handle)
PYEOF
python ../../scripts/check_ios_contract.py openapi.json
rm -f openapi.json

echo
echo "All checks passed."
