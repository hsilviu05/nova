#!/usr/bin/env bash
# Run every check CI runs, in the same order. Faster than pushing and waiting.
set -euo pipefail

cd "$(dirname "$0")/../services/api"

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

echo
echo "All checks passed."
