"""Export one device's history as the ML dataset.

This is the only place the API and the ML pipeline touch. The pipeline never
imports the API; it reads a CSV whose contract is fixed in
``ml/nova_ml/dataset.py`` and mirrored here, and it records the file's
SHA-256 in every model it trains. That hash is the provenance chain, so the
export has to be deterministic for a given database state -- ordered by
time, then by source, with no clock-dependent field in the body.

Two sources are merged:

* the device's telemetry, one row per event;
* the owner's messages to NOVA from the phone, as ``user_message`` rows.

The second is there because "interaction" means more than walking past the
sensor. Somebody who opens the app and talks to their robot is interacting
with it, and a model that could not see that would be predicting presence
rather than engagement.

**An empty export is an error, not an empty file.** The pipeline's first
line of defence is a gate on data volume; a zero-row CSV would pass through
the export, fail that gate, and report "not enough data" one step later than
it should. Better to say so here, where the device id is on the screen.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nova.models.conversation import Conversation, Message
from nova.models.device import Device, DeviceTelemetry
from nova.models.user import User

# Mirrors ml/nova_ml/dataset.py. A change on either side breaks the other's
# tests, which is the point of having the same tuple in two places.
COLUMNS = ("recorded_at", "source", "event_type", "distance_cm", "state")
SOURCE_TELEMETRY = "telemetry"
SOURCE_MESSAGE = "message"
MESSAGE_EVENT_TYPE = "user_message"


class DatasetExportError(Exception):
    """The export cannot be produced. The message says why."""


class DeviceNotExportableError(DatasetExportError):
    """No such device, or nobody owns it yet."""


class EmptyDatasetError(DatasetExportError):
    """There is nothing to export. Named so the CLI can exit distinctly."""


@dataclass(frozen=True, slots=True)
class Manifest:
    device_id: str
    timezone: str
    rows: int
    telemetry_rows: int
    message_rows: int
    first_at: str
    last_at: str
    sha256: str
    exported_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class Export:
    csv_text: str
    manifest: Manifest


def _stamp(value: datetime) -> str:
    # Always UTC, always with an offset. The pipeline refuses naive
    # timestamps, and a local-time export would move every habit by the
    # timezone offset.
    return value.astimezone(UTC).isoformat()


async def export_dataset(session: AsyncSession, device_id: uuid.UUID) -> Export:
    device = await session.get(Device, device_id)
    if device is None or device.user_id is None:
        raise DeviceNotExportableError(f"device {device_id} does not exist or is unclaimed")

    owner = await session.get(User, device.user_id)
    timezone = owner.timezone if owner is not None else "UTC"

    telemetry_rows = (
        await session.execute(
            select(
                DeviceTelemetry.recorded_at,
                DeviceTelemetry.event_type,
                DeviceTelemetry.distance_cm,
                DeviceTelemetry.state,
            )
            .where(DeviceTelemetry.device_id == device_id)
            .order_by(DeviceTelemetry.recorded_at, DeviceTelemetry.id)
        )
    ).all()

    message_rows = (
        await session.execute(
            select(Message.created_at)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == device.user_id, Message.role == "user")
            .order_by(Message.created_at, Message.id)
        )
    ).all()

    # (time, source order, columns). Source order makes the sort total, so
    # a telemetry event and a message at the same instant always land in
    # the same order and the file hashes the same.
    merged: list[tuple[datetime, int, list[str]]] = []
    for recorded_at, event_type, distance_cm, state in telemetry_rows:
        merged.append(
            (
                recorded_at,
                0,
                [
                    _stamp(recorded_at),
                    SOURCE_TELEMETRY,
                    event_type,
                    "" if distance_cm is None else str(int(distance_cm)),
                    state or "",
                ],
            )
        )
    for (created_at,) in message_rows:
        merged.append(
            (created_at, 1, [_stamp(created_at), SOURCE_MESSAGE, MESSAGE_EVENT_TYPE, "", ""])
        )

    if not merged:
        raise EmptyDatasetError(f"device {device_id} has no telemetry and its owner no messages")

    merged.sort(key=lambda item: (item[0], item[1]))

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for _, _, row in merged:
        writer.writerow(row)
    text = buffer.getvalue()

    manifest = Manifest(
        device_id=str(device_id),
        timezone=timezone,
        rows=len(merged),
        telemetry_rows=len(telemetry_rows),
        message_rows=len(message_rows),
        first_at=merged[0][2][0],
        last_at=merged[-1][2][0],
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        exported_at=datetime.now(UTC).isoformat(),
    )
    return Export(csv_text=text, manifest=manifest)
