#!/usr/bin/env python3
"""Every third-party module the code imports must be a declared dependency.

This exists because of a real failure. `anthropic` and `pgvector` were
installed into a development virtualenv and used in the code, but never added
to `pyproject.toml`. Everything passed locally — the venv had them — while
every environment that installed the project from its own metadata was
broken: CI, the Docker image, and anyone cloning the repository.

Six CI jobs failed on it for days, each with a different-looking symptom
(mypy import errors, pytest collapsing at collection, alembic failing to
start, /health never answering), and the one line that mattered was
`ModuleNotFoundError: No module named 'pgvector'`.

An undeclared dependency is invisible to every check that runs inside the
environment that already has it. So this compares imports against the
declared metadata, not against what happens to be installed.

It also distinguishes runtime from the dev extras, because of a second real
failure. `httpx` was declared -- but only under `[dev]`. A new module under
`src/` imported it, every test passed because the test venv installs the
extras, and the production image failed at import with
`ModuleNotFoundError: No module named 'httpx'`. The first version of this
script counted extras as declared and let it through. Now anything imported
under `src/` must be in `dependencies`; `tests/` and `scripts/` may also
use the extras.

    python scripts/check_declared_dependencies.py

Exits non-zero if anything is imported but undeclared.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
SOURCE = SERVICE / "src" / "nova"
TESTS = SERVICE / "tests"
SCRIPTS = SERVICE / "scripts"
PYPROJECT = SERVICE / "pyproject.toml"

# The package's own name, which is not a third-party import.
FIRST_PARTY = {"nova", "tests"}


def imported_modules(root: Path) -> dict[str, str]:
    """Top-level module names imported under ``root``, with where each came from."""
    found: dict[str, str] = {}

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import, which is first-party by
                # definition and has no module name to resolve.
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue

            for name in names:
                top = name.split(".")[0]
                found.setdefault(top, str(path.relative_to(SERVICE)))

    return found


def _names(requirements: list[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        # "sqlalchemy[asyncio]>=2.0.36,<2.1" -> "sqlalchemy"
        name = requirement.split(";")[0].strip()
        for separator in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(separator)[0]
        names.add(normalise(name))
    return names


def declared(project: dict) -> tuple[set[str], set[str]]:
    """(runtime, runtime + extras) distribution names, normalised.

    Kept apart on purpose. Code under ``src/`` ships in the image, which is
    built from ``dependencies`` alone; the extras exist only where somebody
    asked for them.
    """
    runtime = _names(list(project.get("dependencies", [])))
    everything = set(runtime)
    for group in project.get("optional-dependencies", {}).values():
        everything |= _names(list(group))
    return runtime, everything


def normalise(name: str) -> str:
    """PEP 503 name normalisation: `pytest-asyncio` and `pytest_asyncio` are one."""
    return name.lower().replace("_", "-").replace(".", "-")


def main() -> int:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    runtime, everything = declared(project)
    module_to_dist = packages_distributions()

    problems: list[str] = []
    checked = 0

    # What each tree is allowed to import. src/ ships; the rest does not.
    scopes = (
        (SOURCE, runtime, "runtime"),
        (TESTS, everything, "dev"),
        (SCRIPTS, everything, "dev"),
    )

    for root, allowed, label in scopes:
        if not root.exists():  # pragma: no cover - all exist in this repo
            continue

        for module, first_seen in sorted(imported_modules(root).items()):
            if module in FIRST_PARTY or module in sys.stdlib_module_names:
                continue

            checked += 1
            distributions = module_to_dist.get(module)
            if distributions is None:
                # Imported, not stdlib, and not installed at all. Either a
                # typo or a dependency missing from the current environment
                # as well as from the metadata.
                problems.append(f"  {module:24} imported by {first_seen} — not installed")
                continue

            if any(normalise(dist) in allowed for dist in distributions):
                continue

            if label == "runtime" and any(normalise(dist) in everything for dist in distributions):
                problems.append(
                    f"  {module:24} imported by {first_seen} — "
                    f"declared only as a dev extra; src/ ships without the extras"
                )
            else:
                problems.append(
                    f"  {module:24} imported by {first_seen} — "
                    f"provided by {', '.join(sorted(distributions))}, not declared"
                )

    if problems:
        print("Undeclared dependencies:")
        print("\n".join(problems))
        print(
            "\nAdd them to pyproject.toml. Code that imports a package the "
            "project does not declare works only where that package happens "
            "to be installed."
        )
        return 1

    print(f"Dependencies: {checked} third-party imports, all declared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
