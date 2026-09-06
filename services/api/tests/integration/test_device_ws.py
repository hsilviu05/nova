"""The device WebSocket protocol.

Driven through ``httpx-ws``'s ASGI transport rather than Starlette's
``TestClient``. TestClient runs the app in its own event loop and its own
connection, so it would neither share the test's rolled-back transaction --
making a device claimed moments earlier invisible -- nor stay on the loop the
session-scoped engine is bound to. httpx-ws keeps everything in one loop and
one transaction, and still exercises the real handshake and close codes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from httpx_ws import WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nova.models.device import DeviceTelemetry
from nova.schemas.protocol import PROTOCOL_VERSION
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


def _hardware() -> dict[str, str]:
    return {
        "hardware_id": f"esp32s3-{uuid.uuid4().hex[:16]}",
        "model": "ESP32-S3-Touch-AMOLED-2.06",
        "firmware_version": "0.1.0",
    }


def _heartbeat(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "telemetry.heartbeat",
        "version": PROTOCOL_VERSION,
        "id": str(uuid.uuid4()),
        "payload": {"uptime_seconds": 120, "state": "IDLE", **overrides},
    }


@pytest.fixture
def ws_app(settings, engine, session_factory, redis_client) -> FastAPI:
    """The app under test, wired to the shared fixtures.

    A plain fixture rather than an async one: pytest-asyncio runs async
    fixture setup and teardown in different tasks, and the WebSocket
    transport's task group cannot be exited from a task other than the one
    that entered it. Opening the client inside :func:`connect` keeps both ends
    in the test's own task.
    """
    return build_test_app(
        settings, engine=engine, session_factory=session_factory, redis=redis_client
    )


@asynccontextmanager
async def expect_close(code: int) -> AsyncIterator[None]:
    """Assert the server closed the socket with ``code``.

    The transport runs inside nested anyio task groups, so a disconnect
    surfaces wrapped in one or more ``ExceptionGroup`` layers rather than on
    its own. Unwrapping here keeps that plumbing out of every test.
    """
    try:
        yield
    except WebSocketDisconnect as exc:
        assert exc.code == code
    except BaseExceptionGroup as group:
        # Groups nest, so flatten rather than inspecting only the top level.
        matched = group.subgroup(WebSocketDisconnect)
        assert matched is not None, f"expected a WebSocket close, got {group!r}"
        codes = _disconnect_codes(matched)
        assert code in codes, f"expected close {code}, got {codes}"
    else:
        raise AssertionError(f"expected the socket to close with {code}")


def _disconnect_codes(group: BaseExceptionGroup[Any]) -> list[int]:
    """Collect every disconnect code in a possibly nested exception group."""
    codes: list[int] = []
    for exc in group.exceptions:
        if isinstance(exc, WebSocketDisconnect):
            codes.append(exc.code)
        elif isinstance(exc, BaseExceptionGroup):
            codes.extend(_disconnect_codes(exc))
    return codes


@asynccontextmanager
async def connect(
    app: FastAPI, token: str | None = None, *, use_header: bool = False
) -> AsyncIterator[Any]:
    """Open a device WebSocket against ``app``."""
    url = "http://nova.test/api/v1/devices/ws"
    # Must be a dict, never None: httpx_ws calls .update() on whatever is
    # passed without checking it first.
    headers: dict[str, str] = {}

    if token is not None:
        if use_header:
            headers["Authorization"] = f"Bearer {token}"
        else:
            url = f"{url}?token={token}"

    async with (
        AsyncClient(transport=ASGIWebSocketTransport(app), base_url="http://nova.test") as client,
        aconnect_ws(url, client, headers=headers) as socket,
    ):
        yield socket


@pytest.fixture
async def api(ws_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the *same* app instance the sockets use.

    The shared ``client`` fixture builds its own app, and therefore its own
    connection registry. A command posted through it would be dispatched
    against a registry that has never seen the socket opened here, so it
    would report the device offline and the test would wait forever for a
    frame that was never sent.

    Plain ``ASGITransport`` rather than the WebSocket one: it has no task
    group, so it can be torn down from a different task than it was created
    in, which is what pytest-asyncio does with async fixtures.
    """
    async with AsyncClient(
        transport=ASGITransport(app=ws_app), base_url="http://nova.test"
    ) as http_client:
        yield http_client


@pytest.fixture
async def api_auth_headers(api: AsyncClient) -> dict[str, str]:
    """A registered user on the socket-sharing app."""
    body = (
        await api.post(
            "/api/v1/auth/register",
            json={
                "email": f"ws-{uuid.uuid4().hex[:10]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "WS User",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


@pytest.fixture
async def device(api: AsyncClient, api_auth_headers: dict[str, str]) -> dict[str, Any]:
    """A device taken all the way through the claim flow."""
    provisioned = (await api.post("/api/v1/devices/provision", json=_hardware())).json()
    await api.post(
        "/api/v1/devices/claim",
        headers=api_auth_headers,
        json={"code": provisioned["claim_code"], "name": "Nova"},
    )
    collected = (
        await api.post(
            "/api/v1/devices/provision/poll",
            json={"provisioning_token": provisioned["provisioning_token"]},
        )
    ).json()
    return {"id": provisioned["device_id"], "token": collected["device_token"]}


class TestHandshakeAuthentication:
    async def test_accepts_a_valid_credential(
        self, ws_app: FastAPI, device: dict[str, Any]
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(_heartbeat())
            assert (await socket.receive_json())["type"] == "ack"

    async def test_accepts_a_bearer_header(self, ws_app: FastAPI, device: dict[str, Any]) -> None:
        async with connect(ws_app, device["token"], use_header=True) as socket:
            await socket.send_json(_heartbeat())
            assert (await socket.receive_json())["type"] == "ack"

    async def test_rejects_a_missing_token(self, ws_app: FastAPI) -> None:
        # Refused at the handshake: an unauthenticated peer never reaches a
        # state where it can send frames.
        async with expect_close(4001), connect(ws_app):
            pass

    async def test_rejects_an_unknown_token(self, ws_app: FastAPI) -> None:
        async with expect_close(4001), connect(ws_app, "novad_nonsense"):
            pass

    async def test_rejects_a_user_access_token(
        self, ws_app: FastAPI, api_auth_headers: dict[str, str]
    ) -> None:
        """A user JWT is not a device credential, however valid it is."""
        access = api_auth_headers["Authorization"].removeprefix("Bearer ")
        async with expect_close(4001), connect(ws_app, access):
            pass

    async def test_rejects_a_released_device(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        """Deleting a device must invalidate its credential immediately."""
        await api.delete(f"/api/v1/devices/{device['id']}", headers=api_auth_headers)

        async with expect_close(4001), connect(ws_app, device["token"]):
            pass


class TestFrameValidation:
    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "unknown.type", "version": 1, "id": str(uuid.uuid4())},
            {"type": "telemetry.heartbeat", "version": 1},
            {"version": 1, "id": str(uuid.uuid4())},
        ],
    )
    async def test_malformed_frames_are_rejected(
        self, ws_app: FastAPI, device: dict[str, Any], frame: dict[str, Any]
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(frame)
            response = await socket.receive_json()

        assert response["type"] == "error"
        assert response["code"] == "invalid_frame"

    async def test_non_json_is_rejected(self, ws_app: FastAPI, device: dict[str, Any]) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_text("not json at all")
            assert (await socket.receive_json())["type"] == "error"

    async def test_rejects_an_unknown_protocol_version(
        self, ws_app: FastAPI, device: dict[str, Any]
    ) -> None:
        """The version field is what makes upgrading the protocol possible."""
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json({**_heartbeat(), "version": 99})
            assert (await socket.receive_json())["type"] == "error"

    async def test_rejects_unknown_fields(self, ws_app: FastAPI, device: dict[str, Any]) -> None:
        """A command that is almost valid moves real servos."""
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json({**_heartbeat(), "unexpected": "field"})
            assert (await socket.receive_json())["type"] == "error"

    @pytest.mark.parametrize(
        "payload",
        [
            {"battery_percent": 101},
            {"battery_percent": -1},
            {"wifi_rssi": 20},
            {"temperature_c": 300},
            {"uptime_seconds": -5},
        ],
    )
    async def test_rejects_out_of_range_values(
        self, ws_app: FastAPI, device: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(_heartbeat(**payload))
            assert (await socket.receive_json())["type"] == "error"

    async def test_disconnects_after_repeated_violations(
        self, ws_app: FastAPI, device: dict[str, Any]
    ) -> None:
        """A peer that keeps sending garbage is broken or probing."""
        async with expect_close(4003), connect(ws_app, device["token"]) as socket:
            for _ in range(10):
                await socket.send_json({"type": "nonsense"})
                await socket.receive_json()

    async def test_a_valid_frame_clears_the_strike_count(
        self, ws_app: FastAPI, device: dict[str, Any]
    ) -> None:
        """An isolated bad frame must not eventually close a healthy link."""
        async with connect(ws_app, device["token"]) as socket:
            for _ in range(8):
                await socket.send_json({"type": "nonsense"})
                assert (await socket.receive_json())["type"] == "error"

                await socket.send_json(_heartbeat())
                assert (await socket.receive_json())["type"] == "ack"

    async def test_rejects_an_oversized_frame(
        self, ws_app: FastAPI, device: dict[str, Any]
    ) -> None:
        """Checked before parsing, so the decoder never sees it."""
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(
                {
                    "type": "telemetry.event",
                    "version": 1,
                    "id": str(uuid.uuid4()),
                    "payload": {
                        "event_type": "noise",
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "data": {"blob": "x" * 40_000},
                    },
                }
            )
            response = await socket.receive_json()

        assert response["code"] == "frame_too_large"

    async def test_rejects_an_empty_batch(self, ws_app: FastAPI, device: dict[str, Any]) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(
                {
                    "type": "telemetry.batch",
                    "version": 1,
                    "id": str(uuid.uuid4()),
                    "payload": [],
                }
            )
            assert (await socket.receive_json())["type"] == "error"


class TestTelemetryIngestion:
    async def test_heartbeat_marks_the_device_online(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(_heartbeat(battery_percent=78, temperature_c=31.5))
            await socket.receive_json()

        detail = (await api.get(f"/api/v1/devices/{device['id']}", headers=api_auth_headers)).json()
        assert detail["is_online"] is True
        assert detail["last_seen_at"] is not None

        telemetry = (
            await api.get(f"/api/v1/devices/{device['id']}/telemetry", headers=api_auth_headers)
        ).json()
        assert telemetry[0]["event_type"] == "heartbeat"
        assert telemetry[0]["battery_percent"] == 78
        assert telemetry[0]["temperature_c"] == 31.5

    async def test_event_is_stored_with_its_fields(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(
                {
                    "type": "telemetry.event",
                    "version": 1,
                    "id": str(uuid.uuid4()),
                    "payload": {
                        "event_type": "person_detected",
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "distance_cm": 72,
                        "head_yaw": 14,
                        "state": "CURIOUS",
                        "data": {"source": "time_of_flight"},
                    },
                }
            )
            await socket.receive_json()

        telemetry = (
            await api.get(
                f"/api/v1/devices/{device['id']}/telemetry",
                headers=api_auth_headers,
                params={"event_type": "person_detected"},
            )
        ).json()

        assert len(telemetry) == 1
        assert telemetry[0]["distance_cm"] == 72
        assert telemetry[0]["head_yaw"] == 14
        assert telemetry[0]["payload"] == {"source": "time_of_flight"}

    async def test_batch_is_stored(
        self,
        ws_app: FastAPI,
        session: AsyncSession,
        device: dict[str, Any],
    ) -> None:
        """The normal path after any network interruption."""
        recorded = datetime.now(UTC).isoformat()

        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(
                {
                    "type": "telemetry.batch",
                    "version": 1,
                    "id": str(uuid.uuid4()),
                    "payload": [
                        {
                            "event_type": "distance_sample",
                            "recorded_at": recorded,
                            "distance_cm": n,
                        }
                        for n in range(20)
                    ],
                }
            )
            await socket.receive_json()

        count = await session.scalar(
            select(func.count())
            .select_from(DeviceTelemetry)
            .where(DeviceTelemetry.device_id == uuid.UUID(device["id"]))
        )
        assert count == 20

    async def test_recorded_at_is_preserved_distinctly_from_arrival(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        """A flushed backlog must not look like a burst of live activity."""
        recorded = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(
                {
                    "type": "telemetry.event",
                    "version": 1,
                    "id": str(uuid.uuid4()),
                    "payload": {
                        "event_type": "queued_while_offline",
                        "recorded_at": recorded.isoformat(),
                    },
                }
            )
            await socket.receive_json()

        stored = (
            await api.get(
                f"/api/v1/devices/{device['id']}/telemetry",
                headers=api_auth_headers,
                params={"event_type": "queued_while_offline"},
            )
        ).json()[0]

        assert stored["recorded_at"].startswith("2026-01-01T12:00")
        assert stored["received_at"] != stored["recorded_at"]


class TestCommandDispatch:
    """Commands travelling from the owner's API call to the device's socket."""

    async def test_reaches_a_connected_device(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            # Establish the connection before commanding: registration
            # happens on accept, and the first frame proves it completed.
            await socket.send_json(_heartbeat())
            await socket.receive_json()

            response = await api.post(
                f"/api/v1/devices/{device['id']}/commands",
                headers=api_auth_headers,
                json={
                    "command": "head.move",
                    "payload": {"yaw": 20, "pitch": -5, "duration_ms": 500},
                },
            )
            assert response.status_code == 202
            assert response.json()["accepted"] is True

            delivered = await socket.receive_json()

        assert delivered["type"] == "robot.command"
        assert delivered["command"] == "head.move"
        assert delivered["payload"] == {"yaw": 20, "pitch": -5, "duration_ms": 500}
        # The id the API returned is the id the device sees, so a result frame
        # can be correlated back to the request.
        assert delivered["id"] == response.json()["command_id"]

    async def test_expression_command_is_delivered(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(_heartbeat())
            await socket.receive_json()

            await api.post(
                f"/api/v1/devices/{device['id']}/commands",
                headers=api_auth_headers,
                json={
                    "command": "expression.set",
                    "payload": {"emotion": "curious", "intensity": 0.8},
                },
            )
            delivered = await socket.receive_json()

        assert delivered["command"] == "expression.set"
        assert delivered["payload"]["emotion"] == "curious"

    async def test_device_reports_the_result(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        """Acceptance means the frame was sent, not that the servo moved."""
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(_heartbeat())
            await socket.receive_json()

            accepted = (
                await api.post(
                    f"/api/v1/devices/{device['id']}/commands",
                    headers=api_auth_headers,
                    json={"command": "device.restart", "payload": {}},
                )
            ).json()
            await socket.receive_json()

            await socket.send_json(
                {
                    "type": "robot.result",
                    "version": 1,
                    "id": str(uuid.uuid4()),
                    "payload": {"command_id": accepted["command_id"], "ok": True},
                }
            )
            assert (await socket.receive_json())["type"] == "ack"

    async def test_offline_after_disconnect(
        self,
        api: AsyncClient,
        ws_app: FastAPI,
        api_auth_headers: dict[str, str],
        device: dict[str, Any],
    ) -> None:
        """Closing the socket must deregister the device."""
        async with connect(ws_app, device["token"]) as socket:
            await socket.send_json(_heartbeat())
            await socket.receive_json()

        response = await api.post(
            f"/api/v1/devices/{device['id']}/commands",
            headers=api_auth_headers,
            json={"command": "device.restart", "payload": {}},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "device_offline"
