"""Device management: listing, renaming, removal, commands, isolation."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


def _hardware() -> dict[str, str]:
    return {
        "hardware_id": f"esp32s3-{uuid.uuid4().hex[:16]}",
        "model": "ESP32-S3-Touch-AMOLED-2.06",
        "firmware_version": "0.1.0",
    }


async def _claim(
    client: AsyncClient, headers: dict[str, str], name: str | None = None
) -> dict[str, Any]:
    """Provision and claim a device, returning the claimed device."""
    provisioned = (await client.post("/api/v1/devices/provision", json=_hardware())).json()
    body: dict[str, Any] = {"code": provisioned["claim_code"]}
    if name is not None:
        body["name"] = name
    response = await client.post("/api/v1/devices/claim", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _second_user(client: AsyncClient) -> dict[str, str]:
    """Register another account and return its auth headers."""
    body = (
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"other-{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Other",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


class TestListing:
    async def test_lists_only_your_own_devices(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        mine = await _claim(client, auth_headers, name="Mine")
        other_headers = await _second_user(client)
        await _claim(client, other_headers, name="Theirs")

        listing = (await client.get("/api/v1/devices", headers=auth_headers)).json()

        assert [d["id"] for d in listing] == [mine["id"]]

    async def test_empty_before_claiming(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/devices", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/devices")).status_code == 401

    async def test_a_freshly_claimed_device_is_offline(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Online is derived from a heartbeat, and none has arrived."""
        device = await _claim(client, auth_headers)
        assert device["is_online"] is False
        assert device["last_seen_at"] is None


class TestDetail:
    async def test_returns_the_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers, name="Nova")
        response = await client.get(f"/api/v1/devices/{device['id']}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["name"] == "Nova"

    async def test_another_users_device_is_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """404 rather than 403: confirming the id exists would itself leak."""
        device = await _claim(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.get(f"/api/v1/devices/{device['id']}", headers=other_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "device_not_found"

    async def test_unknown_id_is_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get(f"/api/v1/devices/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404

    async def test_malformed_id_is_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/devices/not-a-uuid", headers=auth_headers)
        assert response.status_code == 422


class TestRename:
    async def test_renames(self, client: AsyncClient, auth_headers: dict[str, str]) -> None:
        device = await _claim(client, auth_headers, name="Old")
        response = await client.patch(
            f"/api/v1/devices/{device['id']}",
            headers=auth_headers,
            json={"name": "New"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "New"

    async def test_rejects_a_blank_name(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.patch(
            f"/api/v1/devices/{device['id']}", headers=auth_headers, json={"name": ""}
        )
        assert response.status_code == 422

    async def test_rejects_unknown_fields(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.patch(
            f"/api/v1/devices/{device['id']}",
            headers=auth_headers,
            json={"user_id": str(uuid.uuid4())},
        )
        assert response.status_code == 422

    async def test_cannot_rename_another_users_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.patch(
            f"/api/v1/devices/{device['id']}",
            headers=other_headers,
            json={"name": "Stolen"},
        )
        assert response.status_code == 404


class TestRemoval:
    async def test_removes_the_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)

        assert (
            await client.delete(f"/api/v1/devices/{device['id']}", headers=auth_headers)
        ).status_code == 204

        listing = (await client.get("/api/v1/devices", headers=auth_headers)).json()
        assert listing == []

    async def test_cannot_remove_another_users_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.delete(f"/api/v1/devices/{device['id']}", headers=other_headers)
        assert response.status_code == 404

        # And it is still on the real owner's account.
        listing = (await client.get("/api/v1/devices", headers=auth_headers)).json()
        assert len(listing) == 1

    async def test_removing_twice_is_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        await client.delete(f"/api/v1/devices/{device['id']}", headers=auth_headers)

        response = await client.delete(f"/api/v1/devices/{device['id']}", headers=auth_headers)
        assert response.status_code == 404

    async def test_the_hardware_can_be_claimed_again_afterwards(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Removing a device must not brick the hardware."""
        device = await _claim(client, auth_headers)
        await client.delete(f"/api/v1/devices/{device['id']}", headers=auth_headers)

        reclaimed = await _claim(client, auth_headers, name="Second life")
        assert reclaimed["name"] == "Second life"


class TestCommands:
    async def test_offline_device_reports_503(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Commands are not queued.

        A head movement delivered minutes later, out of context, is worse
        than one that never arrives.
        """
        device = await _claim(client, auth_headers)

        response = await client.post(
            f"/api/v1/devices/{device['id']}/commands",
            headers=auth_headers,
            json={
                "command": "head.move",
                "payload": {"yaw": 20, "pitch": -5, "duration_ms": 500},
            },
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "device_offline"

    @pytest.mark.parametrize(
        "payload",
        [
            {"yaw": 200, "pitch": 0},
            {"yaw": 0, "pitch": 200},
            {"yaw": 0},
            {"yaw": 0, "pitch": 0, "unexpected": 1},
        ],
    )
    async def test_rejects_out_of_range_movement(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        """Servo limits are enforced at the boundary.

        Rejected rather than clamped: silently changing what was asked hides
        the bug that produced it.
        """
        device = await _claim(client, auth_headers)

        response = await client.post(
            f"/api/v1/devices/{device['id']}/commands",
            headers=auth_headers,
            json={"command": "head.move", "payload": payload},
        )
        assert response.status_code == 422

    async def test_rejects_an_unknown_command(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.post(
            f"/api/v1/devices/{device['id']}/commands",
            headers=auth_headers,
            json={"command": "self.destruct", "payload": {}},
        )
        assert response.status_code == 422

    async def test_rejects_an_invalid_expression(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.post(
            f"/api/v1/devices/{device['id']}/commands",
            headers=auth_headers,
            json={"command": "expression.set", "payload": {"emotion": "smug"}},
        )
        assert response.status_code == 422

    async def test_cannot_command_another_users_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        other_headers = await _second_user(client)

        response = await client.post(
            f"/api/v1/devices/{device['id']}/commands",
            headers=other_headers,
            json={"command": "device.restart", "payload": {}},
        )
        # Ownership is checked before connectivity, so this is 404 not 503.
        assert response.status_code == 404


class TestTelemetryAccess:
    async def test_empty_for_a_new_device(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.get(
            f"/api/v1/devices/{device['id']}/telemetry", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.parametrize("limit", [0, 1001, -1])
    async def test_rejects_an_out_of_range_limit(
        self, client: AsyncClient, auth_headers: dict[str, str], limit: int
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.get(
            f"/api/v1/devices/{device['id']}/telemetry",
            headers=auth_headers,
            params={"limit": limit},
        )
        assert response.status_code == 422

    async def test_requires_authentication(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        device = await _claim(client, auth_headers)
        response = await client.get(f"/api/v1/devices/{device['id']}/telemetry")
        assert response.status_code == 401
