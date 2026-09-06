"""Authenticated user routes and bearer-token enforcement."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from nova.core.config import JWTSettings, Settings
from nova.core.security import TokenService

pytestmark = pytest.mark.integration


class TestReadMe:
    async def test_returns_the_authenticated_user(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered: dict[str, Any],
    ) -> None:
        response = await client.get("/api/v1/users/me", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["id"] == registered["user"]["id"]

    async def test_never_exposes_the_password_hash(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        body = (await client.get("/api/v1/users/me", headers=auth_headers)).json()
        assert "password_hash" not in body
        assert "password" not in body


class TestAuthenticationEnforcement:
    async def test_rejects_missing_header(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/users/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "missing_credentials"

    async def test_rejects_malformed_token(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer garbage"})
        assert response.status_code == 401

    async def test_rejects_wrong_scheme(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Basic {registered['tokens']['access_token']}"},
        )
        assert response.status_code == 401

    async def test_rejects_a_refresh_token_used_as_a_bearer(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        response = await client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {registered['tokens']['refresh_token']}"},
        )
        assert response.status_code == 401

    async def test_rejects_token_signed_with_another_key(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        forger = TokenService(
            JWTSettings(secret_key="an-entirely-different-key-of-sufficient-size")  # type: ignore[arg-type]
        )
        forged = forger.issue_access_token(uuid.uuid4())
        response = await client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {forged.token}"}
        )
        assert response.status_code == 401

    async def test_rejects_valid_token_for_a_nonexistent_user(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        # Correctly signed, but the subject has no row: authentication must
        # check the database, not trust the signature alone.
        tokens = TokenService(settings.jwt)
        orphan = tokens.issue_access_token(uuid.uuid4())
        response = await client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {orphan.token}"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "account_unavailable"


class TestUpdateMe:
    async def test_updates_display_name(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"display_name": "Renamed"},
        )
        assert response.status_code == 200
        assert response.json()["display_name"] == "Renamed"

    async def test_ignores_omitted_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered: dict[str, Any],
    ) -> None:
        response = await client.patch("/api/v1/users/me", headers=auth_headers, json={})
        assert response.status_code == 200
        assert response.json()["display_name"] == registered["user"]["display_name"]

    async def test_rejects_blank_display_name(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"display_name": ""}
        )
        assert response.status_code == 422

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.patch("/api/v1/users/me", json={"display_name": "Nobody"})
        assert response.status_code == 401
