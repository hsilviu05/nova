"""GitHub dev mode, end to end: the owner wires it up, GitHub delivers, the
device reacts.

The device is a recording transport registered directly on the app's
connection registry, so the assertion is on the exact frames that would
have gone down the socket.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from nova.services.connections import InMemoryConnectionRegistry
from nova.services.github_webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    sign,
)
from tests.conftest import build_test_app
from tests.integration.test_devices import _claim

pytestmark = pytest.mark.integration


class RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, object]) -> None:
        self.frames.append(dict(data))


@pytest.fixture
def registry() -> InMemoryConnectionRegistry:
    return InMemoryConnectionRegistry()


@pytest.fixture
async def api(settings, engine, session_factory, redis_client, registry):  # type: ignore[no-untyped-def]
    app = build_test_app(
        settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis_client,
        connections=registry,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://nova.test") as client:
        yield client


async def _register(api: AsyncClient) -> dict[str, str]:
    body = (
        await api.post(
            "/api/v1/auth/register",
            json={
                "email": f"gh-{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Dev",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


def green(repo: str = "hsilviu05/nova") -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {"conclusion": "success", "name": "CI"},
        "repository": {"full_name": repo},
    }


def red() -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {"conclusion": "failure"},
        "repository": {"full_name": "x/y"},
    }


async def deliver(
    api: AsyncClient,
    url: str,
    secret: str,
    payload: dict[str, Any],
    *,
    event: str = "workflow_run",
    delivery: str | None = None,
    signature: str | None = None,
):  # type: ignore[no-untyped-def]
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery or str(uuid.uuid4()),
        SIGNATURE_HEADER: signature if signature is not None else sign(secret, body),
    }
    return await api.post(url, content=body, headers=headers)


class TestLifecycle:
    async def test_create_shows_the_secret_once(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = await api.post("/api/v1/integrations/github", headers=headers, json={})
        assert created.status_code == 201, created.text
        body = created.json()
        assert len(body["secret"]) >= 32
        assert body["webhook_url"].endswith(f"/api/v1/integrations/github/webhook/{body['id']}")
        assert body["enabled"] is True

        read = (await api.get("/api/v1/integrations/github", headers=headers)).json()
        assert "secret" not in read
        assert read["id"] == body["id"]

    async def test_rotation_invalidates_the_old_secret(self, api: AsyncClient) -> None:
        headers = await _register(api)
        first = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()
        second = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()
        assert second["id"] == first["id"]
        assert second["secret"] != first["secret"]

        response = await deliver(api, first["webhook_url"], first["secret"], green())
        assert response.status_code == 401
        response = await deliver(api, second["webhook_url"], second["secret"], green())
        assert response.status_code == 200

    async def test_update_and_delete(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        updated = await api.patch(
            "/api/v1/integrations/github",
            headers=headers,
            json={"repository": "hsilviu05/nova", "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json() == {
            **updated.json(),
            "repository": "hsilviu05/nova",
            "enabled": False,
        }

        assert (await api.delete("/api/v1/integrations/github", headers=headers)).status_code == 204
        assert (await api.get("/api/v1/integrations/github", headers=headers)).status_code == 404
        # The webhook URL dies with it.
        response = await deliver(api, created["webhook_url"], created["secret"], green())
        assert response.status_code == 404

    async def test_a_bad_repository_name_is_rejected(self, api: AsyncClient) -> None:
        headers = await _register(api)
        response = await api.post(
            "/api/v1/integrations/github", headers=headers, json={"repository": "not a repo"}
        )
        assert response.status_code == 422

    async def test_another_user_sees_nothing(self, api: AsyncClient) -> None:
        mine = await _register(api)
        theirs = await _register(api)
        await api.post("/api/v1/integrations/github", headers=mine, json={})
        assert (await api.get("/api/v1/integrations/github", headers=theirs)).status_code == 404

    async def test_requires_authentication(self, api: AsyncClient) -> None:
        assert (await api.get("/api/v1/integrations/github")).status_code == 401


class TestDelivery:
    async def test_a_green_build_makes_a_happy_face_and_says_so(
        self, api: AsyncClient, registry: InMemoryConnectionRegistry
    ) -> None:
        headers = await _register(api)
        device = await _claim(api, headers)
        transport = RecordingTransport()
        await registry.register(uuid.UUID(device["id"]), transport)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        response = await deliver(api, created["webhook_url"], created["secret"], green())
        assert response.status_code == 200, response.text
        assert response.json() == {
            "status": "reacted",
            "reason": None,
            "reaction": "ci_passed",
            "devices_reached": 1,
        }

        assert [f["command"] for f in transport.frames] == ["expression.set", "speaker.play"]
        assert transport.frames[0]["payload"] == {"emotion": "happy", "intensity": 1.0}
        assert transport.frames[1]["payload"] == {"text": "Build's green.", "emotion": "happy"}
        # Real protocol frames, with ids the device can acknowledge.
        for frame in transport.frames:
            assert frame["type"] == "robot.command"
            assert frame["version"] == 1
            uuid.UUID(frame["id"])

        read = (await api.get("/api/v1/integrations/github", headers=headers)).json()
        assert read["last_event"] == "workflow_run:success"
        assert read["last_delivery_at"] is not None

    async def test_a_red_build_is_confused(
        self, api: AsyncClient, registry: InMemoryConnectionRegistry
    ) -> None:
        headers = await _register(api)
        device = await _claim(api, headers)
        transport = RecordingTransport()
        await registry.register(uuid.UUID(device["id"]), transport)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        response = await deliver(api, created["webhook_url"], created["secret"], red())
        assert response.json()["reaction"] == "ci_failed"
        assert transport.frames[0]["payload"]["emotion"] == "confused"

    async def test_an_offline_device_is_skipped_not_queued(self, api: AsyncClient) -> None:
        headers = await _register(api)
        await _claim(api, headers)  # claimed, never connected
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        response = await deliver(api, created["webhook_url"], created["secret"], green())
        assert response.status_code == 200
        assert response.json()["status"] == "reacted"
        assert response.json()["devices_reached"] == 0

    async def test_every_connected_device_reacts(
        self, api: AsyncClient, registry: InMemoryConnectionRegistry
    ) -> None:
        headers = await _register(api)
        transports = []
        for _ in range(2):
            device = await _claim(api, headers)
            transport = RecordingTransport()
            await registry.register(uuid.UUID(device["id"]), transport)
            transports.append(transport)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        response = await deliver(api, created["webhook_url"], created["secret"], green())
        assert response.json()["devices_reached"] == 2
        assert all(len(t.frames) == 2 for t in transports)

    async def test_the_owner_of_another_device_is_not_reached(
        self, api: AsyncClient, registry: InMemoryConnectionRegistry
    ) -> None:
        mine = await _register(api)
        theirs = await _register(api)
        their_device = await _claim(api, theirs)
        transport = RecordingTransport()
        await registry.register(uuid.UUID(their_device["id"]), transport)
        created = (await api.post("/api/v1/integrations/github", headers=mine, json={})).json()

        await deliver(api, created["webhook_url"], created["secret"], green())
        assert transport.frames == []


class TestRefusals:
    async def test_a_bad_signature_is_401_and_nothing_moves(
        self, api: AsyncClient, registry: InMemoryConnectionRegistry
    ) -> None:
        headers = await _register(api)
        device = await _claim(api, headers)
        transport = RecordingTransport()
        await registry.register(uuid.UUID(device["id"]), transport)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        for signature in ("", "sha256=" + "0" * 64, sign("wrong", json.dumps(green()).encode())):
            response = await deliver(
                api, created["webhook_url"], created["secret"], green(), signature=signature
            )
            assert response.status_code == 401
        assert transport.frames == []

    async def test_a_tampered_body_is_401(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()
        signed_for = json.dumps(green()).encode()
        tampered = json.dumps(red()).encode()
        response = await api.post(
            created["webhook_url"],
            content=tampered,
            headers={
                EVENT_HEADER: "workflow_run",
                DELIVERY_HEADER: "d1",
                SIGNATURE_HEADER: sign(created["secret"], signed_for),
            },
        )
        assert response.status_code == 401

    async def test_an_unknown_integration_is_404_without_reading_the_body(
        self, api: AsyncClient
    ) -> None:
        response = await api.post(
            f"/api/v1/integrations/github/webhook/{uuid.uuid4()}",
            content=b"{}",
            headers={SIGNATURE_HEADER: "sha256=" + "0" * 64},
        )
        assert response.status_code == 404

    async def test_an_oversized_body_is_413(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()
        huge = b"{" + b" " * (MAX_BODY_BYTES + 1) + b"}"
        response = await api.post(
            created["webhook_url"],
            content=huge,
            headers={EVENT_HEADER: "ping", SIGNATURE_HEADER: sign(created["secret"], huge)},
        )
        assert response.status_code == 413

    async def test_a_replayed_delivery_is_ignored(
        self, api: AsyncClient, registry: InMemoryConnectionRegistry
    ) -> None:
        headers = await _register(api)
        device = await _claim(api, headers)
        transport = RecordingTransport()
        await registry.register(uuid.UUID(device["id"]), transport)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        first = await deliver(
            api, created["webhook_url"], created["secret"], green(), delivery="same-id"
        )
        again = await deliver(
            api, created["webhook_url"], created["secret"], green(), delivery="same-id"
        )
        assert first.json()["status"] == "reacted"
        assert again.json() == {
            "status": "ignored",
            "reason": "duplicate delivery",
            "reaction": None,
            "devices_reached": 0,
        }
        assert len(transport.frames) == 2  # once, not twice

    async def test_a_disabled_integration_acknowledges_and_ignores(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()
        await api.patch("/api/v1/integrations/github", headers=headers, json={"enabled": False})

        response = await deliver(api, created["webhook_url"], created["secret"], green())
        assert response.status_code == 200
        assert response.json()["reason"] == "integration disabled"

    async def test_an_unwatched_repository_is_ignored(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (
            await api.post(
                "/api/v1/integrations/github",
                headers=headers,
                json={"repository": "hsilviu05/nova"},
            )
        ).json()

        response = await deliver(
            api, created["webhook_url"], created["secret"], green("someone/else")
        )
        assert response.json()["reason"] == "repository not watched"
        response = await deliver(
            api, created["webhook_url"], created["secret"], green("hsilviu05/nova")
        )
        assert response.json()["status"] == "reacted"

    async def test_ping_and_unknown_events_are_200(self, api: AsyncClient) -> None:
        # GitHub sends a ping on creation, and retries anything non-2xx.
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()
        response = await deliver(
            api, created["webhook_url"], created["secret"], {"zen": "ok"}, event="ping"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        response = await deliver(
            api, created["webhook_url"], created["secret"], {"action": "opened"}, event="issues"
        )
        assert response.status_code == 200
