#!/usr/bin/env python3
"""Validate firmware-produced frames against the server's own schema.

The device protocol has two implementations of one wire format: Pydantic
models in `services/api/src/nova/schemas/protocol.py`, and C++ in
`firmware/core/src/protocol.cpp`. Nothing but this stops them drifting.

It works in both directions:

* **Device to server** -- builds `emit_frames`, runs it, and validates every
  frame it prints with the real Pydantic models. A field the firmware
  misspells, a number it sends as a string, an enum value the server does not
  accept: all rejected here rather than by a device in someone's kitchen.
* **Server to device** -- regenerates the fixtures the C++ tests decode, so
  they stay frames the server actually produces rather than frames somebody
  wrote by hand on the firmware side.

    python scripts/check_protocol_contract.py            # both directions
    python scripts/check_protocol_contract.py --emit     # fixtures only

Exits non-zero on a mismatch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "firmware"
FIXTURES = FIRMWARE / "test" / "fixtures" / "inbound.json"

sys.path.insert(0, str(ROOT / "services/api/src"))

from pydantic import TypeAdapter, ValidationError  # noqa: E402

from nova.schemas.protocol import (  # noqa: E402
    AckFrame,
    ExpressionCommand,
    ExpressionPayload,
    HeadMoveCommand,
    HeadMovePayload,
    InboundFrame,
    ProtocolErrorFrame,
    RestartCommand,
    SleepCommand,
    SleepPayload,
    SpeakCommand,
    SpeakPayload,
)

# The server parses whatever a device sends as this union, so validating
# against it is exactly the check the backend performs on a live connection.
_inbound = TypeAdapter(InboundFrame)


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(f"{n:08x}-0000-4000-8000-000000000000")


def emit_fixtures() -> None:
    """Write the server-to-device frames the C++ tests decode."""
    frames = {
        "head_move": HeadMoveCommand(
            id=_id(1), payload=HeadMovePayload(yaw=30, pitch=-15, duration_ms=800)
        ),
        "head_move_defaults": HeadMoveCommand(
            id=_id(2), payload=HeadMovePayload(yaw=0, pitch=0)
        ),
        "expression": ExpressionCommand(
            id=_id(3), payload=ExpressionPayload(emotion="curious", intensity=0.6)
        ),
        "expression_defaults": ExpressionCommand(
            id=_id(4), payload=ExpressionPayload(emotion="happy")
        ),
        "speak": SpeakCommand(
            id=_id(5), payload=SpeakPayload(text="Hello there.", emotion="happy")
        ),
        "speak_no_emotion": SpeakCommand(
            id=_id(6), payload=SpeakPayload(text="Just this.")
        ),
        "sleep": SleepCommand(id=_id(7), payload=SleepPayload(duration_seconds=300)),
        "restart": RestartCommand(id=_id(8)),
        "ack": AckFrame(id=_id(9), ref=_id(1)),
        "error": ProtocolErrorFrame(
            id=_id(10), ref=_id(1), code="bad_frame", message="nope"
        ),
    }

    FIXTURES.parent.mkdir(parents=True, exist_ok=True)
    FIXTURES.write_text(
        json.dumps(
            {name: frame.model_dump_json() for name, frame in frames.items()}, indent=2
        )
        + "\n"
    )
    print(f"  wrote {len(frames)} fixtures to {FIXTURES.relative_to(ROOT)}")


def build_emitter(build_dir: Path) -> Path:
    """Compile the firmware's frame emitter."""
    sources = [
        FIRMWARE / "tools" / "emit_frames.cpp",
        FIRMWARE / "core" / "src" / "protocol.cpp",
        FIRMWARE / "core" / "src" / "behaviour.cpp",
        FIRMWARE / "third_party" / "cJSON" / "cJSON.c",
    ]
    binary = build_dir / "emit_frames"

    subprocess.run(  # noqa: S603
        [
            "g++",
            "-std=c++20",
            "-O0",
            f"-I{FIRMWARE / 'core' / 'include'}",
            f"-I{FIRMWARE / 'third_party' / 'cJSON'}",
            "-x",
            "c++",  # cJSON.c compiled as C++ so one command links cleanly
            *[str(path) for path in sources],
            "-o",
            str(binary),
        ],
        check=True,
    )
    return binary


def check_outbound(binary: Path) -> bool:
    """Every frame the firmware emits must satisfy the server's schema."""
    result = subprocess.run(  # noqa: S603
        [str(binary)], capture_output=True, text=True, check=True
    )

    ok = True
    checked = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, raw = line.partition("\t")
        checked += 1
        try:
            _inbound.validate_json(raw)
        except ValidationError as exc:
            ok = False
            print(f"  {name:32} REJECTED BY THE SERVER SCHEMA")
            for error in exc.errors()[:4]:
                location = ".".join(str(part) for part in error["loc"])
                print(f"      {location}: {error['msg']}")
            print(f"      frame: {raw[:160]}")

    if ok:
        print(f"  {checked} firmware frames accepted by the server schema")
    return ok


def main() -> int:
    if "--emit" in sys.argv:
        emit_fixtures()
        return 0

    print("Protocol contract")
    emit_fixtures()

    if shutil.which("g++") is None:
        print("  g++ not found; cannot check the firmware side")
        return 2

    with tempfile.TemporaryDirectory() as scratch:
        try:
            binary = build_emitter(Path(scratch))
        except subprocess.CalledProcessError:
            print("  firmware emitter failed to build")
            return 1

        ok = check_outbound(binary)

    print("\nProtocol contract:", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
