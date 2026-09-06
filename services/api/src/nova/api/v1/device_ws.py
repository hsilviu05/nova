"""The device WebSocket endpoint.

Authentication happens at the handshake, before the socket is accepted: an
unauthenticated peer never reaches a state where it can send frames.

Every frame is validated against the discriminated union in
:mod:`nova.schemas.protocol`. Malformed frames are answered with an error
frame and counted; a peer that keeps sending them is disconnected, since at
that point it is either badly broken or probing.

Each frame runs in its own database transaction. A long-lived socket cannot
hold one open across the connection's lifetime -- that would pin a pool
connection for hours and turn one bad frame into a rollback of everything
since the device connected.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketState

from nova.core.config import Settings
from nova.core.errors import AuthenticationError
from nova.core.logging import get_logger
from nova.models.device import Device
from nova.repositories.device import (
    DeviceCredentialRepository,
    DeviceRepository,
    DeviceTelemetryRepository,
)
from nova.schemas.protocol import (
    AckFrame,
    CommandResultFrame,
    HeartbeatFrame,
    InboundFrame,
    ProtocolErrorFrame,
    TelemetryBatchFrame,
    TelemetryEventFrame,
)
from nova.services.connections import ConnectionRegistry
from nova.services.device import DeviceService

logger = get_logger(__name__)

router = APIRouter(tags=["devices"])

_inbound_adapter: TypeAdapter[InboundFrame] = TypeAdapter(InboundFrame)

# Close codes. 4001 and 4003 are in the private range reserved for the
# application, so firmware can distinguish "your credential is bad, stop
# retrying" from "the network dropped, reconnect".
CLOSE_UNAUTHORISED = 4001
CLOSE_PROTOCOL_VIOLATION = 4003


def _bearer_token(websocket: WebSocket) -> str | None:
    """Extract the device token from the handshake.

    Prefers the Authorization header. Falls back to a query parameter because
    the ESP-IDF WebSocket client cannot always set custom headers, and a token
    in a query string is at least still inside TLS. It is logged nowhere.
    """
    header = websocket.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None

    token = websocket.query_params.get("token")
    return token.strip() or None if token else None


@router.websocket("/devices/ws")
async def device_socket(websocket: WebSocket) -> None:
    """Serve one device connection."""
    settings: Settings = websocket.app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = websocket.app.state.session_factory
    connections: ConnectionRegistry = websocket.app.state.connections

    token = _bearer_token(websocket)
    if token is None:
        # Refuse before accepting: an unauthenticated peer never gets a
        # socket it can send frames on.
        await websocket.close(code=CLOSE_UNAUTHORISED)
        return

    try:
        async with session_factory() as session:
            device = await _authenticate(session, connections, token)
            await session.commit()
            device_id = device.id
            device_name = device.name
    except AuthenticationError:
        logger.info("device_ws_rejected", reason="invalid_credential")
        await websocket.close(code=CLOSE_UNAUTHORISED)
        return

    await websocket.accept()
    await connections.register(device_id, websocket)

    structlog.contextvars.bind_contextvars(device_id=str(device_id))
    logger.info("device_connected", device_name=device_name)

    violations = 0
    try:
        while True:
            raw = await websocket.receive_text()

            if len(raw) > settings.device.max_frame_bytes:
                # Checked before parsing so an oversized payload is never
                # handed to the JSON decoder.
                violations += 1
                await _send_error(
                    websocket, None, "frame_too_large", "Frame exceeds the size limit."
                )
                if violations >= settings.device.max_protocol_violations:
                    await websocket.close(code=CLOSE_PROTOCOL_VIOLATION)
                    return
                continue

            try:
                frame = _inbound_adapter.validate_json(raw)
            except ValidationError as exc:
                violations += 1
                logger.info(
                    "device_frame_rejected",
                    errors=exc.error_count(),
                    violations=violations,
                )
                await _send_error(websocket, None, "invalid_frame", "Frame failed validation.")
                if violations >= settings.device.max_protocol_violations:
                    logger.warning("device_disconnected_protocol_violations")
                    await websocket.close(code=CLOSE_PROTOCOL_VIOLATION)
                    return
                continue

            # A valid frame clears the strike count: an isolated bad frame
            # during a firmware update should not eventually close a
            # connection that has since recovered.
            violations = 0

            async with session_factory() as session:
                try:
                    await _handle_frame(session, connections, device_id, frame)
                except Exception:
                    await session.rollback()
                    raise
                else:
                    await session.commit()

            await websocket.send_json(AckFrame(ref=frame.id).model_dump(mode="json"))

    except WebSocketDisconnect:
        logger.info("device_disconnected")
    except Exception:
        logger.exception("device_connection_error")
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=CLOSE_PROTOCOL_VIOLATION)
    finally:
        # Conditional, so a closing socket cannot evict the replacement that
        # already displaced it after a reconnect.
        await connections.unregister_if_current(device_id, websocket)
        structlog.contextvars.unbind_contextvars("device_id")


async def _authenticate(
    session: AsyncSession, connections: ConnectionRegistry, token: str
) -> Device:
    """Resolve the handshake token to a device."""
    service = DeviceService(
        devices=DeviceRepository(session),
        credentials=DeviceCredentialRepository(session),
        telemetry=DeviceTelemetryRepository(session),
        connections=connections,
    )
    return await service.authenticate(token)


async def _handle_frame(
    session: AsyncSession,
    connections: ConnectionRegistry,
    device_id: uuid.UUID,
    frame: InboundFrame,
) -> None:
    """Apply one validated frame."""
    devices = DeviceRepository(session)
    device = await devices.get_by_id(device_id)
    if device is None:  # pragma: no cover - deleted mid-connection
        return

    service = DeviceService(
        devices=devices,
        credentials=DeviceCredentialRepository(session),
        telemetry=DeviceTelemetryRepository(session),
        connections=connections,
    )

    if isinstance(frame, HeartbeatFrame):
        await service.record_heartbeat(device, frame.payload)
    elif isinstance(frame, TelemetryEventFrame):
        await service.record_events(device, [frame.payload])
    elif isinstance(frame, TelemetryBatchFrame):
        count = await service.record_events(device, frame.payload)
        logger.info("device_telemetry_batch", events=count)
    elif isinstance(frame, CommandResultFrame):
        # Recorded as telemetry rather than mutating command state: there is
        # no command table yet, and the outcome is still worth keeping.
        logger.info(
            "device_command_result",
            command_id=str(frame.payload.command_id),
            ok=frame.payload.ok,
            detail=frame.payload.detail,
        )


async def _send_error(websocket: WebSocket, ref: uuid.UUID | None, code: str, message: str) -> None:
    """Send a protocol error frame, ignoring a socket that has already gone."""
    payload: dict[str, Any] = ProtocolErrorFrame(ref=ref, code=code, message=message).model_dump(
        mode="json"
    )
    try:
        await websocket.send_json(payload)
    except Exception:
        return
