"""The device claim flow, end to end.

The security property under test throughout: reading the code off a device's
screen lets you claim it to your own account, and never lets you impersonate
the device. Those are two different secrets doing two different jobs.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nova.core.clock import utc_now
from nova.core.device_security import (
    DEVICE_TOKEN_PREFIX,
    hash_provisioning_token,
)
from nova.core.errors import AuthenticationError
from nova.models.device import DeviceClaim
from nova.repositories.device import (
    DeviceCredentialRepository,
    DeviceRepository,
    DeviceTelemetryRepository,
)
from nova.services.connections import InMemoryConnectionRegistry
from nova.services.device import DeviceService

pytestmark = pytest.mark.integration


def _hardware(prefix: str = "esp32s3") -> dict[str, str]:
    return {
        "hardware_id": f"{prefix}-{uuid.uuid4().hex[:16]}",
        "model": "ESP32-S3-Touch-AMOLED-2.06",
        "firmware_version": "0.1.0",
    }


async def _provision(client: AsyncClient, **overrides: str) -> dict[str, Any]:
    response = await client.post("/api/v1/devices/provision", json={**_hardware(), **overrides})
    assert response.status_code == 201, response.text
    return response.json()


class TestProvision:
    async def test_returns_a_code_and_a_token(self, client: AsyncClient) -> None:
        body = await _provision(client)

        assert body["claim_code"]
        assert body["provisioning_token"]
        assert body["device_id"]
        assert body["poll_interval_seconds"] > 0

    async def test_code_is_human_transcribable(self, client: AsyncClient) -> None:
        code = (await _provision(client))["claim_code"]

        assert len(code) == 7 and code[3] == "-"
        # Ambiguous glyphs are excluded so a code read off a small screen
        # transcribes cleanly.
        assert not set(code) & set("ILOU01")

    async def test_needs_no_authentication(self, client: AsyncClient) -> None:
        # The device has no credential at this point; requiring one would be
        # a chicken-and-egg problem.
        response = await client.post("/api/v1/devices/provision", json=_hardware())
        assert response.status_code == 201

    async def test_codes_are_unique(self, client: AsyncClient) -> None:
        codes = {(await _provision(client))["claim_code"] for _ in range(10)}
        assert len(codes) == 10

    @pytest.mark.parametrize(
        "override",
        [
            {"hardware_id": "short"},
            {"hardware_id": "has spaces in it"},
            {"model": ""},
        ],
    )
    async def test_rejects_invalid_payloads(
        self, client: AsyncClient, override: dict[str, str]
    ) -> None:
        response = await client.post("/api/v1/devices/provision", json={**_hardware(), **override})
        assert response.status_code == 422

    async def test_rejects_unknown_fields(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/devices/provision",
            json={**_hardware(), "is_admin": True},
        )
        assert response.status_code == 422

    async def test_same_hardware_reuses_one_device_row(self, client: AsyncClient) -> None:
        hardware = _hardware()
        first = await _provision(client, **hardware)
        second = await _provision(client, **hardware)
        assert first["device_id"] == second["device_id"]

    async def test_reprovisioning_retires_the_previous_code(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """A code still readable on an earlier screen must stop working."""
        hardware = _hardware()
        first = await _provision(client, **hardware)
        await _provision(client, **hardware)

        response = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": first["claim_code"]},
        )
        assert response.status_code == 404


class TestClaim:
    async def test_binds_the_device_to_the_user(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        provisioned = await _provision(client)

        response = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"], "name": "Nova"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["id"] == provisioned["device_id"]
        assert body["name"] == "Nova"
        assert body["claimed_at"] is not None

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        provisioned = await _provision(client)
        response = await client.post(
            "/api/v1/devices/claim", json={"code": provisioned["claim_code"]}
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("transform", [str.lower, lambda c: c.replace("-", "")])
    async def test_accepts_forgiving_input(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        transform: Any,
    ) -> None:
        """People type lower case and drop the hyphen."""
        provisioned = await _provision(client)
        response = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": transform(provisioned["claim_code"])},
        )
        assert response.status_code == 201

    async def test_defaults_the_name_to_the_model(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        provisioned = await _provision(client)
        response = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )
        assert response.json()["name"] == "ESP32-S3-Touch-AMOLED-2.06"

    async def test_rejects_an_unknown_code(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/devices/claim", headers=auth_headers, json={"code": "AAA-AAA"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invalid_claim_code"

    async def test_a_code_works_only_once(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        provisioned = await _provision(client)
        payload = {"code": provisioned["claim_code"]}

        first = await client.post("/api/v1/devices/claim", headers=auth_headers, json=payload)
        second = await client.post("/api/v1/devices/claim", headers=auth_headers, json=payload)

        assert first.status_code == 201
        assert second.status_code == 404

    async def test_used_and_unknown_codes_are_indistinguishable(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Otherwise the endpoint enumerates which codes exist."""
        provisioned = await _provision(client)
        await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )

        used = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )
        unknown = await client.post(
            "/api/v1/devices/claim", headers=auth_headers, json={"code": "AAA-AAA"}
        )

        assert used.status_code == unknown.status_code == 404
        assert used.json()["error"] | {"request_id": None} == unknown.json()["error"] | {
            "request_id": None
        }

    async def test_rejects_an_expired_code(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        session: AsyncSession,
    ) -> None:
        provisioned = await _provision(client)

        claim = (
            await session.execute(
                select(DeviceClaim).where(
                    DeviceClaim.provisioning_token_hash
                    == hash_provisioning_token(provisioned["provisioning_token"])
                )
            )
        ).scalar_one()
        # Backdate rather than sleeping through the TTL.
        claim.expires_at = utc_now()
        await session.flush()

        response = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )
        assert response.status_code == 404


class TestCollect:
    async def test_pending_until_claimed(self, client: AsyncClient) -> None:
        provisioned = await _provision(client)

        response = await client.post(
            "/api/v1/devices/provision/poll",
            json={"provisioning_token": provisioned["provisioning_token"]},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"

    async def test_returns_a_credential_after_claiming(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        provisioned = await _provision(client)
        await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"], "name": "Nova"},
        )

        response = await client.post(
            "/api/v1/devices/provision/poll",
            json={"provisioning_token": provisioned["provisioning_token"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "claimed"
        assert body["device_token"].startswith(DEVICE_TOKEN_PREFIX)
        assert body["device_id"] == provisioned["device_id"]
        assert body["heartbeat_interval_seconds"] > 0

    async def test_credential_is_returned_exactly_once(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """A repeat collection means a device bug or a stolen token."""
        provisioned = await _provision(client)
        await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )
        payload = {"provisioning_token": provisioned["provisioning_token"]}

        first = await client.post("/api/v1/devices/provision/poll", json=payload)
        second = await client.post("/api/v1/devices/provision/poll", json=payload)

        assert first.status_code == 200
        assert second.status_code == 401

    async def test_rejects_an_unknown_token(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/devices/provision/poll",
            json={"provisioning_token": "not-a-real-token"},
        )
        assert response.status_code == 401

    async def test_claiming_does_not_reveal_the_provisioning_token(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The core security property of the two-secret design.

        Someone who reads the code off the screen can claim the device to
        their account. They must not thereby obtain anything that lets them
        speak *as* the device.
        """
        provisioned = await _provision(client)

        claim_response = await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )

        body = claim_response.text
        assert provisioned["provisioning_token"] not in body
        assert "device_token" not in claim_response.json()


class TestReprovisioning:
    async def test_transfers_ownership_and_kills_old_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        session: AsyncSession,
    ) -> None:
        """Physical possession is authority to reset, as on consumer hardware.

        A resold or recovered NOVA must stop reporting to its previous owner.
        """
        hardware = _hardware()
        provisioned = await _provision(client, **hardware)
        await client.post(
            "/api/v1/devices/claim",
            headers=auth_headers,
            json={"code": provisioned["claim_code"]},
        )
        collected = (
            await client.post(
                "/api/v1/devices/provision/poll",
                json={"provisioning_token": provisioned["provisioning_token"]},
            )
        ).json()
        old_token = collected["device_token"]

        # Someone factory-resets the device.
        await _provision(client, **hardware)

        # It is no longer on the original owner's account.
        listing = await client.get("/api/v1/devices", headers=auth_headers)
        assert listing.json() == []

        # And the old credential no longer authenticates.
        service = DeviceService(
            devices=DeviceRepository(session),
            credentials=DeviceCredentialRepository(session),
            telemetry=DeviceTelemetryRepository(session),
            connections=InMemoryConnectionRegistry(),
        )
        with pytest.raises(AuthenticationError):
            await service.authenticate(old_token)
