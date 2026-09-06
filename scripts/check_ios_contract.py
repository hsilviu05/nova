#!/usr/bin/env python3
"""Check the iOS models against the API's OpenAPI schema.

The iOS app cannot be compiled in CI without a macOS runner, so this is the
one automated check standing between a backend schema change and a client
that silently decodes the wrong thing. It compares each Swift model's stored
properties with the schema it has to decode, after applying the same
snake_case-to-camelCase conversion the decoder uses.

    python scripts/check_ios_contract.py                  # against a running API
    python scripts/check_ios_contract.py path/to/openapi.json

Exits non-zero on a mismatch.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_SPEC_URL = "http://127.0.0.1:8000/openapi.json"
MODELS_DIR = Path(__file__).resolve().parent.parent / "apps/ios/NOVA/Models"

# Swift struct -> the OpenAPI schema it decodes.
MODELS = {
    "User": "UserRead",
    "TokenPair": "TokenPair",
    "Device": "DeviceRead",
    "TelemetryEvent": "TelemetryRead",
    "CommandAccepted": "CommandAccepted",
    "Conversation": "ConversationRead",
    "ConversationDetail": "ConversationDetail",
    "ChatMessage": "MessageRead",
}


def camel(name: str) -> str:
    """Match JSONDecoder's .convertFromSnakeCase."""
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def load_spec(source: str) -> dict:
    if source.startswith("http"):
        with urllib.request.urlopen(source, timeout=10) as response:  # noqa: S310
            return json.load(response)
    return json.loads(Path(source).read_text())


def swift_sources() -> str:
    return "\n".join(path.read_text() for path in sorted(MODELS_DIR.glob("*.swift")))


def stored_properties(sources: str, struct: str) -> set[str] | None:
    """The `let` properties declared directly on a struct."""
    match = re.search(rf"^struct {struct}\b[^{{]*\{{(.*?)^\}}", sources, re.S | re.M)
    if match is None:
        return None
    return set(re.findall(r"^    let (\w+):", match.group(1), re.M))


def swift_emotions(sources: str) -> set[str]:
    """Cases of the Expression enum, which mirrors a server Literal."""
    match = re.search(r"enum Expression\b[^{]*\{(.*?)\n    \}", sources, re.S)
    if match is None:
        return set()

    cases: set[str] = set()
    for line in re.findall(r"^\s*case (.+)$", match.group(1), re.M):
        cases.update(part.strip() for part in line.split(","))
    return {case for case in cases if case}


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SPEC_URL

    try:
        spec = load_spec(source)
    except Exception as exc:  # noqa: BLE001 - any failure is the same message
        print(f"Could not read the OpenAPI spec from {source}: {exc}")
        print("Start the API first, or pass a path to a saved openapi.json.")
        return 2

    schemas = spec["components"]["schemas"]
    sources = swift_sources()
    ok = True

    for struct, schema_name in MODELS.items():
        schema = schemas.get(schema_name)
        if schema is None:
            print(f"  {struct:<16} schema {schema_name} missing from the spec")
            ok = False
            continue

        server = {camel(name) for name in schema.get("properties", {})}
        required = {camel(name) for name in schema.get("required", [])}
        app = stored_properties(sources, struct)

        if app is None:
            print(f"  {struct:<16} not found in {MODELS_DIR.name}/")
            ok = False
            continue

        # Required but unmodelled means the app silently drops data the
        # server always sends. Unknown means the app expects something that
        # will never arrive, which fails decoding outright.
        missing = required - app
        unknown = app - server
        optional_absent = (server - app) - required

        status = "OK" if not (missing or unknown) else "MISMATCH"
        print(f"  {struct:<16} vs {schema_name:<16} {status}")

        if missing:
            print(f"      required, not modelled: {sorted(missing)}")
        if unknown:
            print(f"      modelled, not in schema: {sorted(unknown)}")
        if optional_absent:
            print(f"      optional, not modelled: {sorted(optional_absent)}")

        ok = ok and not (missing or unknown)

    expression = (
        schemas.get("ExpressionPayload", {}).get("properties", {}).get("emotion", {})
    )
    server_emotions = set(expression.get("enum", []))
    app_emotions = swift_emotions(sources)

    if server_emotions:
        matches = server_emotions == app_emotions
        print(f"\n  {'emotions':<16} {'OK' if matches else 'MISMATCH'}")
        if not matches:
            print(f"      server only: {sorted(server_emotions - app_emotions)}")
            print(f"      app only:    {sorted(app_emotions - server_emotions)}")
            ok = False

    print("\nContract check:", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
