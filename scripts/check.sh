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

echo
echo "All checks passed."
