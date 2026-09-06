"""Authentication flows end to end, against a real database."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


class TestRegistration:
    async def test_creates_account_and_returns_tokens(
        self, client: AsyncClient, credentials: dict[str, str]
    ) -> None:
        response = await client.post("/api/v1/auth/register", json=credentials)

        assert response.status_code == 201
        body = response.json()
        assert body["user"]["email"] == credentials["email"]
        assert body["user"]["display_name"] == credentials["display_name"]
        assert body["user"]["is_active"] is True
        assert body["tokens"]["access_token"]
        assert body["tokens"]["refresh_token"]
        assert body["tokens"]["token_type"] == "bearer"
        assert body["tokens"]["expires_in"] > 0

    async def test_never_returns_the_password_hash(
        self, client: AsyncClient, credentials: dict[str, str]
    ) -> None:
        response = await client.post("/api/v1/auth/register", json=credentials)
        assert "password" not in response.text.lower().replace("password_", "")
        assert "hash" not in response.json()["user"]

    async def test_rejects_duplicate_email(
        self, client: AsyncClient, credentials: dict[str, str]
    ) -> None:
        await client.post("/api/v1/auth/register", json=credentials)
        response = await client.post("/api/v1/auth/register", json=credentials)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "email_already_registered"

    async def test_email_is_case_insensitive(
        self, client: AsyncClient, credentials: dict[str, str]
    ) -> None:
        await client.post("/api/v1/auth/register", json=credentials)
        shouting = {**credentials, "email": credentials["email"].upper()}
        response = await client.post("/api/v1/auth/register", json=shouting)
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "override",
        [
            {"email": "not-an-email"},
            {"password": "short"},
            {"display_name": ""},
            {"password": "   " * 5},
        ],
    )
    async def test_rejects_invalid_payloads(
        self, client: AsyncClient, credentials: dict[str, str], override: dict[str, str]
    ) -> None:
        response = await client.post("/api/v1/auth/register", json={**credentials, **override})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_rejects_missing_fields(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/auth/register", json={})
        assert response.status_code == 422


class TestLogin:
    async def test_accepts_correct_credentials(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": credentials["password"]},
        )
        assert response.status_code == 200
        assert response.json()["user"]["id"] == registered["user"]["id"]

    async def test_accepts_differently_cased_email(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": credentials["email"].upper(),
                "password": credentials["password"],
            },
        )
        assert response.status_code == 200

    async def test_rejects_wrong_password(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": "definitely-wrong"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_unknown_email_is_indistinguishable_from_wrong_password(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        # Identical status and body, so the response cannot be used to
        # enumerate which emails have accounts.
        unknown = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "definitely-wrong"},
        )
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": "definitely-wrong"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]
        assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]

    async def test_401_carries_www_authenticate(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": "definitely-wrong"},
        )
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_records_last_login(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": credentials["email"], "password": credentials["password"]},
        )
        assert response.json()["user"]["last_login_at"] is not None

    async def test_each_login_issues_distinct_tokens(
        self, client: AsyncClient, credentials: dict[str, str], registered: dict[str, Any]
    ) -> None:
        payload = {"email": credentials["email"], "password": credentials["password"]}
        first = (await client.post("/api/v1/auth/login", json=payload)).json()
        second = (await client.post("/api/v1/auth/login", json=payload)).json()
        assert first["tokens"]["refresh_token"] != second["tokens"]["refresh_token"]


class TestRefreshRotation:
    async def test_returns_a_new_pair(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        original = registered["tokens"]["refresh_token"]
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})

        assert response.status_code == 200
        assert response.json()["refresh_token"] != original

    async def test_rotated_token_still_authenticates(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        rotated = (
            await client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": registered["tokens"]["refresh_token"]},
            )
        ).json()

        me = await client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {rotated['access_token']}"},
        )
        assert me.status_code == 200

    async def test_old_token_stops_working_after_rotation(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        original = registered["tokens"]["refresh_token"]
        await client.post("/api/v1/auth/refresh", json={"refresh_token": original})

        replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "invalid_refresh_token"

    async def test_replay_revokes_the_whole_family(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        """The security property that makes rotation worth having.

        Replaying a retired token means someone holds a copy they should not.
        The successor token must die with it, not keep working.
        """
        original = registered["tokens"]["refresh_token"]
        successor = (
            await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        ).json()["refresh_token"]

        # Attacker replays the stolen, already-rotated token.
        replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401

        # The legitimate user's live token is now dead too.
        victim = await client.post("/api/v1/auth/refresh", json={"refresh_token": successor})
        assert victim.status_code == 401

    async def test_rotation_chains(self, client: AsyncClient, registered: dict[str, Any]) -> None:
        token = registered["tokens"]["refresh_token"]
        for _ in range(5):
            response = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
            assert response.status_code == 200
            token = response.json()["refresh_token"]

    async def test_rejects_unknown_token(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"}
        )
        assert response.status_code == 401

    async def test_rejects_an_access_token(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        # An access token is not a refresh token, even though both are strings.
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered["tokens"]["access_token"]},
        )
        assert response.status_code == 401


class TestLogout:
    async def test_revokes_the_refresh_token(
        self, client: AsyncClient, registered: dict[str, Any]
    ) -> None:
        token = registered["tokens"]["refresh_token"]
        assert (
            await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        ).status_code == 204

        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401

    async def test_is_idempotent(self, client: AsyncClient, registered: dict[str, Any]) -> None:
        token = registered["tokens"]["refresh_token"]
        first = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        second = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        assert first.status_code == second.status_code == 204

    async def test_unknown_token_returns_204(self, client: AsyncClient) -> None:
        # Reporting "unknown" would let a caller probe which tokens exist.
        response = await client.post("/api/v1/auth/logout", json={"refresh_token": "never-issued"})
        assert response.status_code == 204

    async def test_logout_all_kills_every_session(
        self,
        client: AsyncClient,
        credentials: dict[str, str],
        registered: dict[str, Any],
    ) -> None:
        second_session = (
            await client.post(
                "/api/v1/auth/login",
                json={
                    "email": credentials["email"],
                    "password": credentials["password"],
                },
            )
        ).json()

        response = await client.post(
            "/api/v1/auth/logout-all",
            headers={"Authorization": f"Bearer {registered['tokens']['access_token']}"},
        )
        assert response.status_code == 204

        for session in (registered, second_session):
            replay = await client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": session["tokens"]["refresh_token"]},
            )
            assert replay.status_code == 401

    async def test_logout_all_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/auth/logout-all")).status_code == 401
