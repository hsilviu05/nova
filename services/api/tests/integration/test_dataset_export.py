"""The ML dataset export.

The rows here are a handful of hand-placed events that prove the export
merges, orders, formats and hashes correctly. They are not a dataset and
nothing is trained on them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nova.models.conversation import Conversation, Message
from nova.models.device import Device, DeviceTelemetry
from nova.models.user import User
from nova.services.dataset_export import (
    COLUMNS,
    DeviceNotExportableError,
    EmptyDatasetError,
    export_dataset,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


async def _owner(session: AsyncSession) -> User:
    user = User(
        email=f"export-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="not-a-real-hash",
        display_name="Owner",
        timezone="Europe/Bucharest",
    )
    session.add(user)
    await session.flush()
    return user


async def _device(session: AsyncSession, owner: User | None) -> Device:
    device = Device(
        hardware_id=f"esp32s3-{uuid.uuid4().hex[:12]}",
        model="ESP32-S3-Touch-AMOLED-2.06",
        user_id=owner.id if owner else None,
        claimed_at=T0 if owner else None,
    )
    session.add(device)
    await session.flush()
    return device


def _telemetry(device: Device, minutes: int, event_type: str, **fields: object) -> DeviceTelemetry:
    return DeviceTelemetry(
        device_id=device.id,
        event_type=event_type,
        recorded_at=T0 + timedelta(minutes=minutes),
        **fields,
    )


class TestExport:
    async def test_merges_orders_and_formats(self, session: AsyncSession) -> None:
        owner = await _owner(session)
        device = await _device(session, owner)
        # Inserted out of order on purpose.
        session.add_all(
            [
                _telemetry(device, 30, "heartbeat", state="idle"),
                _telemetry(device, 10, "person_detected", distance_cm=62, state="curious"),
                _telemetry(device, 20, "device.state", state="listening"),
            ]
        )
        conversation = Conversation(user_id=owner.id)
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                Message(
                    conversation_id=conversation.id,
                    role="user",
                    content="hello",
                    created_at=T0 + timedelta(minutes=15),
                ),
                # The reply is not an interaction by the user.
                Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content="hi",
                    created_at=T0 + timedelta(minutes=16),
                ),
            ]
        )
        await session.flush()

        export = await export_dataset(session, device.id)
        rows = list(csv.reader(io.StringIO(export.csv_text)))

        assert tuple(rows[0]) == COLUMNS
        body = rows[1:]
        assert [r[2] for r in body] == [
            "person_detected",
            "user_message",
            "device.state",
            "heartbeat",
        ]
        # Chronological, UTC, with an explicit offset -- the pipeline
        # refuses naive timestamps.
        stamps = [r[0] for r in body]
        assert stamps == sorted(stamps)
        assert all(s.endswith("+00:00") for s in stamps)
        # Absent readings are empty, not zero.
        assert body[0][3] == "62"
        assert body[3][3] == ""
        assert body[1][1] == "message" and body[1][4] == ""

        m = export.manifest
        assert (m.rows, m.telemetry_rows, m.message_rows) == (4, 3, 1)
        assert m.timezone == "Europe/Bucharest"
        assert m.first_at == stamps[0] and m.last_at == stamps[-1]
        assert m.sha256 == hashlib.sha256(export.csv_text.encode()).hexdigest()

    async def test_is_deterministic(self, session: AsyncSession) -> None:
        # The hash is the provenance chain; two exports of the same state
        # must be byte-identical.
        owner = await _owner(session)
        device = await _device(session, owner)
        session.add(_telemetry(device, 0, "person_detected", distance_cm=50))
        await session.flush()

        first = await export_dataset(session, device.id)
        second = await export_dataset(session, device.id)
        assert first.csv_text == second.csv_text
        assert first.manifest.sha256 == second.manifest.sha256

    async def test_another_users_messages_are_not_included(self, session: AsyncSession) -> None:
        owner = await _owner(session)
        stranger = await _owner(session)
        device = await _device(session, owner)
        session.add(_telemetry(device, 0, "heartbeat"))
        conversation = Conversation(user_id=stranger.id)
        session.add(conversation)
        await session.flush()
        session.add(Message(conversation_id=conversation.id, role="user", content="not mine"))
        await session.flush()

        export = await export_dataset(session, device.id)
        assert export.manifest.message_rows == 0


class TestRefusals:
    async def test_unknown_device(self, session: AsyncSession) -> None:
        with pytest.raises(DeviceNotExportableError):
            await export_dataset(session, uuid.uuid4())

    async def test_unclaimed_device(self, session: AsyncSession) -> None:
        device = await _device(session, None)
        with pytest.raises(DeviceNotExportableError):
            await export_dataset(session, device.id)

    async def test_nothing_to_export_is_an_error_not_an_empty_file(
        self, session: AsyncSession
    ) -> None:
        owner = await _owner(session)
        device = await _device(session, owner)
        with pytest.raises(EmptyDatasetError):
            await export_dataset(session, device.id)
