"""Health, readiness, and the shared error envelope."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from nova import __version__

pytestmark = pytest.mark.integration


class TestHealth:
    async def test_reports_ok(self, client: AsyncClient) -> None:
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["environment"] == "test"

    async def test_does_not_require_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/health")).status_code == 200


class TestReadiness:
    async def test_reports_every_dependency_healthy(self, client: AsyncClient) -> None:
        response = await client.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert {d["name"] for d in body["dependencies"]} == {"postgres", "redis"}
        assert all(d["healthy"] for d in body["dependencies"])
        assert all(d["latency_ms"] is not None for d in body["dependencies"])

    async def test_returns_503_when_a_dependency_is_down(
        self, degraded_client: AsyncClient
    ) -> None:
        # The app's Redis points at a closed port. The probe must report the
        # failure rather than propagate an exception.
        response = await degraded_client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        redis_status = next(d for d in body["dependencies"] if d["name"] == "redis")
        assert redis_status["healthy"] is False
        # Postgres is fine, so readiness must pinpoint the failure rather
        # than reporting a blanket outage.
        postgres_status = next(d for d in body["dependencies"] if d["name"] == "postgres")
        assert postgres_status["healthy"] is True


class TestErrorEnvelope:
    async def test_unknown_route_uses_the_envelope(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/does-not-exist")

        assert response.status_code == 404
        assert set(response.json()["error"]) >= {"code", "message", "request_id"}

    async def test_every_response_carries_a_request_id(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.headers.get("x-request-id")

    async def test_request_ids_are_unique_per_request(self, client: AsyncClient) -> None:
        first = await client.get("/health")
        second = await client.get("/health")
        assert first.headers["x-request-id"] != second.headers["x-request-id"]

    async def test_client_supplied_request_id_is_not_trusted(self, client: AsyncClient) -> None:
        # Accepting it would let a caller forge log correlation.
        response = await client.get("/health", headers={"X-Request-ID": "attacker-controlled"})
        assert response.headers["x-request-id"] != "attacker-controlled"

    async def test_error_body_matches_the_error_header(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/users/me")
        body: dict[str, Any] = response.json()
        assert body["error"]["request_id"] == response.headers["x-request-id"]
