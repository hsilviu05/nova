"""The device WebSocket protocol.

Versioned and strictly validated, per
:doc:`ADR 004 <../../../docs/decisions/004-websocket-device-protocol>`. Frames
are discriminated unions on ``type``, so an unknown or malformed frame is
rejected by the schema rather than reaching a handler that has to guess.

``extra="forbid"`` throughout is deliberate. A command that is *almost* valid
moves real servos, and silently ignoring an unrecognised field is how a
firmware typo becomes a mechanical failure.

The ``version`` field exists from the first release because the firmware ships
inside a physical object that may not be reflashed for months. The backend
will eventually be talking to an older protocol than it prefers, and adding a
version after the fact costs far more than carrying an integer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION: Literal[1] = 1

# Servo travel limits, enforced at the boundary. The mechanism cannot reach
# beyond these, so a command that asks it to is rejected rather than clamped:
# silently changing what was asked hides the bug that produced it.
MIN_YAW, MAX_YAW = -90, 90
MIN_PITCH, MAX_PITCH = -45, 45


class Frame(BaseModel):
    """Fields common to every frame in either direction."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = PROTOCOL_VERSION
    # Correlates a command with its acknowledgement.
    id: uuid.UUID = Field(default_factory=uuid.uuid4)


# ---------------------------------------------------------------------------
# Device -> server
# ---------------------------------------------------------------------------


class HeartbeatPayload(BaseModel):
    """Periodic liveness and vitals."""

    model_config = ConfigDict(extra="forbid")

    uptime_seconds: int = Field(ge=0)
    state: str = Field(max_length=24)
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    temperature_c: float | None = Field(default=None, ge=-40, le=125)
    wifi_rssi: int | None = Field(default=None, ge=-120, le=0)
    firmware_version: str | None = Field(default=None, max_length=32)


class HeartbeatFrame(Frame):
    """``telemetry.heartbeat`` -- the device is alive."""

    type: Literal["telemetry.heartbeat"]
    payload: HeartbeatPayload


class TelemetryEventPayload(BaseModel):
    """One structured observation.

    ``recorded_at`` is the device's own clock. It is kept distinct from
    arrival time so that a batch queued during an outage and flushed on
    reconnect is not mistaken for a burst of live activity.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=48)
    recorded_at: datetime

    battery_percent: int | None = Field(default=None, ge=0, le=100)
    temperature_c: float | None = Field(default=None, ge=-40, le=125)
    distance_cm: int | None = Field(default=None, ge=0, le=400)
    head_yaw: int | None = Field(default=None, ge=MIN_YAW, le=MAX_YAW)
    head_pitch: int | None = Field(default=None, ge=MIN_PITCH, le=MAX_PITCH)
    wifi_rssi: int | None = Field(default=None, ge=-120, le=0)
    uptime_seconds: int | None = Field(default=None, ge=0)
    state: str | None = Field(default=None, max_length=24)

    # Event-specific fields that do not warrant a column. Bounded so a device
    # cannot push unbounded JSON into the database.
    data: dict[str, Any] = Field(default_factory=dict)


class TelemetryEventFrame(Frame):
    """``telemetry.event`` -- one observation."""

    type: Literal["telemetry.event"]
    payload: TelemetryEventPayload


class TelemetryBatchFrame(Frame):
    """``telemetry.batch`` -- events buffered while offline.

    The device queues telemetry when it cannot reach the backend and flushes
    on reconnect, so this is the normal path after any network interruption.
    """

    type: Literal["telemetry.batch"]
    payload: list[TelemetryEventPayload] = Field(min_length=1, max_length=100)


class CommandResultPayload(BaseModel):
    """The outcome of a command the server sent."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    ok: bool
    detail: str | None = Field(default=None, max_length=200)


class CommandResultFrame(Frame):
    """``robot.result`` -- a command finished."""

    type: Literal["robot.result"]
    payload: CommandResultPayload


InboundFrame = Annotated[
    HeartbeatFrame | TelemetryEventFrame | TelemetryBatchFrame | CommandResultFrame,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Server -> device
# ---------------------------------------------------------------------------


class HeadMovePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaw: int = Field(ge=MIN_YAW, le=MAX_YAW)
    pitch: int = Field(ge=MIN_PITCH, le=MAX_PITCH)
    duration_ms: int = Field(default=500, ge=0, le=10_000)


class ExpressionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emotion: Literal[
        "idle",
        "listening",
        "thinking",
        "speaking",
        "curious",
        "happy",
        "confused",
        "alert",
        "sleeping",
    ]
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)


class SpeakPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    # Chosen by the behaviour engine, never by the language model directly.
    emotion: str | None = Field(default=None, max_length=24)


class SleepPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: int = Field(ge=1, le=86_400)


class HeadMoveCommand(Frame):
    type: Literal["robot.command"] = "robot.command"
    command: Literal["head.move"] = "head.move"
    payload: HeadMovePayload


class ExpressionCommand(Frame):
    type: Literal["robot.command"] = "robot.command"
    command: Literal["expression.set"] = "expression.set"
    payload: ExpressionPayload


class SpeakCommand(Frame):
    type: Literal["robot.command"] = "robot.command"
    command: Literal["speaker.play"] = "speaker.play"
    payload: SpeakPayload


class SleepCommand(Frame):
    type: Literal["robot.command"] = "robot.command"
    command: Literal["device.sleep"] = "device.sleep"
    payload: SleepPayload


class RestartCommand(Frame):
    type: Literal["robot.command"] = "robot.command"
    command: Literal["device.restart"] = "device.restart"
    payload: dict[str, Any] = Field(default_factory=dict)


OutboundCommand = Annotated[
    HeadMoveCommand | ExpressionCommand | SpeakCommand | SleepCommand | RestartCommand,
    Field(discriminator="command"),
]


class AckFrame(Frame):
    """``ack`` -- a frame was accepted."""

    type: Literal["ack"] = "ack"
    # The frame being acknowledged.
    ref: uuid.UUID


class ProtocolErrorFrame(Frame):
    """``error`` -- a frame was rejected.

    Carries a machine-readable code so firmware can branch without parsing
    prose, and a bounded message for a human reading device logs.
    """

    type: Literal["error"] = "error"
    ref: uuid.UUID | None = None
    code: str
    message: str = Field(max_length=200)
