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
    "Memory": "MemoryRead",
    "MemoryPage": "MemoryPage",
    "MemorySearchResult": "MemorySearchResult",
    "Analytics": "AnalyticsRead",
    "Insights": "InsightsRead",
    "Insight": "InsightRead",
    "Coverage": "CoverageRead",
    "HourBucket": "HourBucket",
    "DayBucket": "DayBucket",
    "WeekdayHourBucket": "WeekdayHourBucket",
    "BatteryPoint": "BatteryPoint",
    "EventTypeBucket": "EventTypeBucket",
    "Gap": "GapRead",
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


def swift_memory_categories(sources: str) -> set[str]:
    """Cases of MemoryCategory, which mirrors a server Literal.

    ``unknown`` is excluded: it is the app's own fallback for a category the
    server adds later, not something the API ever sends as a valid value.
    """
    # Matched to the enum's closing brace rather than to a blank line, so a
    # case declared below one is still seen. The four-space indent is what
    # separates declarations from the `case`s inside the switch bodies.
    match = re.search(r"enum MemoryCategory\b[^{]*\{(.*?)^\}", sources, re.S | re.M)
    if match is None:
        return set()

    cases: set[str] = set()
    for line in re.findall(r"^    case (.+)$", match.group(1), re.M):
        cases.update(part.strip() for part in line.split(","))
    return {case for case in cases if case and case != "unknown"}


def swift_nested_enum_cases(sources: str, outer: str, inner: str) -> set[str]:
    """Cases of an enum nested inside a struct, e.g. Insight.Confidence."""
    outer_body = re.search(
        rf"^struct {outer}\b[^{{]*\{{(.*?)^\}}", sources, re.S | re.M
    )
    if outer_body is None:
        return set()

    match = re.search(
        rf"enum {inner}\b[^{{]*\{{(.*?)\n    \}}", outer_body.group(1), re.S
    )
    if match is None:
        return set()

    cases: set[str] = set()
    for line in re.findall(r"^\s*case (.+)$", match.group(1), re.M):
        cases.update(part.strip() for part in line.split(","))
    # A `switch self` inside the enum has `case` lines too. Declarations are
    # bare identifiers; switch arms start with a dot or carry a colon.
    return {
        case for case in cases if case and not case.startswith(".") and ":" not in case
    }


def check_enum(label: str, server: set[str], app: set[str]) -> bool:
    """Compare a server Literal with the enum the app mirrors it in."""
    if not server:
        return True

    matches = server == app
    print(f"\n  {label:<16} {'OK' if matches else 'MISMATCH'}")
    if not matches:
        print(f"      server only: {sorted(server - app)}")
        print(f"      app only:    {sorted(app - server)}")
    return matches


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

    ok = check_enum("emotions", server_emotions, app_emotions) and ok

    # The categories the app offers have to be exactly the ones the API will
    # accept, or a correction saved from the picker comes back a 422.
    update_category = (
        schemas.get("MemoryUpdate", {}).get("properties", {}).get("category", {})
    )
    server_categories = {
        value
        for option in update_category.get("anyOf", [update_category])
        for value in option.get("enum", []) or []
        if isinstance(value, str)
    }
    ok = (
        check_enum("categories", server_categories, swift_memory_categories(sources))
        and ok
    )

    # Confidence is a server Literal too. A value the app cannot decode
    # fails the whole insights response, not one card.
    confidence = (
        schemas.get("InsightRead", {}).get("properties", {}).get("confidence", {})
    )
    ok = (
        check_enum(
            "confidence",
            set(confidence.get("enum", [])),
            swift_nested_enum_cases(sources, "Insight", "Confidence"),
        )
        and ok
    )

    print("\nContract check:", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
