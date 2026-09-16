"""GitHub dev mode, end to end: the owner wires it up, GitHub delivers, and
NOVA records what happened.

The reaction used to be a face and a spoken line pushed to every connected
device. There is no device, so it is now recorded on the integration -- which
is what the iOS settings screen reads. The classifier that decides *what* the
reaction is did not change, and neither did the refusal ladder in front of
it, which is most of what these tests are about.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from nova.services.github_webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    sign,
)
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(settings, engine, session_factory, redis_client):  # type: ignore[no-untyped-def]
    app = build_test_app(
        settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis_client,
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
    async def test_a_green_build_is_recorded_in_plain_words(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        response = await deliver(api, created["webhook_url"], created["secret"], green())
        assert response.status_code == 200, response.text
        assert response.json() == {
            "status": "recorded",
            "reason": None,
            "reaction": "ci_passed",
            "detail": "Build's green.",
        }

        # The sentence was written to be spoken aloud by a robot. It reads
        # just as well on the settings screen, which is where it goes now.
        read = (await api.get("/api/v1/integrations/github", headers=headers)).json()
        assert read["last_event"] == "Build's green."
        assert read["last_delivery_at"] is not None

    async def test_a_red_build_is_classified_differently(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        response = await deliver(api, created["webhook_url"], created["secret"], red())

        assert response.json()["reaction"] == "ci_failed"
        assert response.json()["detail"] != "Build's green."

    async def test_a_delivery_for_one_owner_does_not_touch_another(self, api: AsyncClient) -> None:
        """Two accounts, two integrations, one delivery.

        The webhook URL carries an integration id and nothing else, so this
        is the test that the id is the only thing consulted -- the other
        account's integration must be untouched.
        """
        mine = await _register(api)
        theirs = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=mine, json={})).json()
        await api.post("/api/v1/integrations/github", headers=theirs, json={})

        await deliver(api, created["webhook_url"], created["secret"], green())

        assert (await api.get("/api/v1/integrations/github", headers=mine)).json()[
            "last_delivery_at"
        ] is not None
        assert (await api.get("/api/v1/integrations/github", headers=theirs)).json()[
            "last_delivery_at"
        ] is None


class TestRefusals:
    async def test_a_bad_signature_is_401_and_nothing_is_recorded(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        for signature in ("", "sha256=" + "0" * 64, sign("wrong", json.dumps(green()).encode())):
            response = await deliver(
                api, created["webhook_url"], created["secret"], green(), signature=signature
            )
            assert response.status_code == 401

        # Refused before anything is written: last_delivery_at stays null.
        read = (await api.get("/api/v1/integrations/github", headers=headers)).json()
        assert read["last_delivery_at"] is None

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

    async def test_a_replayed_delivery_is_ignored(self, api: AsyncClient) -> None:
        headers = await _register(api)
        created = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        first = await deliver(
            api, created["webhook_url"], created["secret"], green(), delivery="same-id"
        )
        again = await deliver(
            api, created["webhook_url"], created["secret"], green(), delivery="same-id"
        )

        assert first.json()["status"] == "recorded"
        assert again.json() == {
            "status": "ignored",
            "reason": "duplicate delivery",
            "reaction": None,
            "detail": None,
        }

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
        assert response.json()["status"] == "recorded"

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


class TestBodiesThatAreSignedAndStillWrong:
    """Signed by GitHub and still not a payload NOVA can read.

    Not a thing that happens in practice, which is precisely why it needs a
    test: the failure mode is a 500, and GitHub retries a non-2xx delivery
    for hours.
    """

    async def _wired(self, api: AsyncClient) -> dict[str, Any]:
        headers = await _register(api)
        return (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

    async def _deliver_raw(self, api: AsyncClient, created: dict[str, Any], body: bytes) -> Any:
        return await api.post(
            created["webhook_url"],
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "workflow_run",
                DELIVERY_HEADER: str(uuid.uuid4()),
                SIGNATURE_HEADER: sign(created["secret"], body),
            },
        )

    async def test_a_body_that_is_not_json_is_acknowledged_and_ignored(
        self, api: AsyncClient
    ) -> None:
        """Acknowledged, not refused. A 4xx here would put GitHub into a
        retry storm over a delivery that will never parse."""
        created = await self._wired(api)

        response = await self._deliver_raw(api, created, b"not json at all")

        assert response.status_code == 200
        assert response.json()["reason"] == "body is not JSON"

    @pytest.mark.parametrize("body", [b"[]", b'"a string"', b"42", b"null"])
    async def test_valid_json_that_is_not_an_object_is_ignored(
        self, api: AsyncClient, body: bytes
    ) -> None:
        created = await self._wired(api)

        response = await self._deliver_raw(api, created, body)

        assert response.status_code == 200
        assert response.json()["reason"] == "body is not an object"

    async def test_an_oversized_declared_length_is_refused_before_the_body_is_read(
        self, api: AsyncClient
    ) -> None:
        """The cheap refusal.

        A Content-Length over the cap costs nothing to reject; reading the
        body first would mean a client could make NOVA buffer whatever it
        liked by lying about nothing at all.
        """
        created = await self._wired(api)
        huge = b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}"

        response = await api.post(
            created["webhook_url"],
            content=huge,
            headers={
                EVENT_HEADER: "ping",
                DELIVERY_HEADER: str(uuid.uuid4()),
                SIGNATURE_HEADER: sign(created["secret"], huge),
                "Content-Length": str(len(huge)),
            },
        )

        assert response.status_code == 413

    async def test_a_body_larger_than_it_declared_is_still_refused(self, api: AsyncClient) -> None:
        """A chunked delivery declares no length at all.

        The declared-length check is an optimisation; this is the bound that
        actually holds, and it is the one a client cannot talk its way past.
        """
        created = await self._wired(api)
        huge = b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}"

        async def chunks() -> Any:
            yield huge

        response = await api.post(
            created["webhook_url"],
            content=chunks(),
            headers={
                EVENT_HEADER: "ping",
                DELIVERY_HEADER: str(uuid.uuid4()),
                SIGNATURE_HEADER: sign(created["secret"], huge),
            },
        )

        assert response.status_code == 413


class TestManagingAnIntegrationThatIsNotThere:
    """Every route has to answer 404 rather than raise.

    They share one repository lookup that returns None, and each caller has
    to turn that into the same answer -- which is the kind of thing that
    stays right only if each one is asserted.
    """

    async def test_reading_one(self, api: AsyncClient) -> None:
        headers = await _register(api)

        assert (await api.get("/api/v1/integrations/github", headers=headers)).status_code == 404

    async def test_updating_one(self, api: AsyncClient) -> None:
        headers = await _register(api)
        response = await api.patch(
            "/api/v1/integrations/github", headers=headers, json={"enabled": False}
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "github_integration_not_found"

    async def test_deleting_one(self, api: AsyncClient) -> None:
        headers = await _register(api)

        assert (await api.delete("/api/v1/integrations/github", headers=headers)).status_code == 404


class TestRotationKeepsWhatWasConfigured:
    async def test_rotating_with_a_new_repository_changes_it(self, api: AsyncClient) -> None:
        headers = await _register(api)
        first = (
            await api.post(
                "/api/v1/integrations/github",
                headers=headers,
                json={"repository": "hsilviu05/nova"},
            )
        ).json()

        second = (
            await api.post(
                "/api/v1/integrations/github",
                headers=headers,
                json={"repository": "hsilviu05/snapworth"},
            )
        ).json()

        assert second["id"] == first["id"]
        assert second["repository"] == "hsilviu05/snapworth"

    async def test_rotating_without_naming_one_keeps_the_existing_repository(
        self, api: AsyncClient
    ) -> None:
        """Rotating a secret is not a reason to widen what is watched.

        Clearing it would silently start accepting deliveries from every
        repository the hook is installed on.
        """
        headers = await _register(api)
        await api.post(
            "/api/v1/integrations/github",
            headers=headers,
            json={"repository": "hsilviu05/nova"},
        )

        rotated = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        assert rotated["repository"] == "hsilviu05/nova"

    async def test_updating_only_the_repository_leaves_enabled_alone(
        self, api: AsyncClient
    ) -> None:
        """A PATCH is a partial update.

        Omitting ``enabled`` must not read as "set it to null", or changing
        the watched repository would quietly switch the integration off.
        """
        headers = await _register(api)
        await api.post("/api/v1/integrations/github", headers=headers, json={})
        await api.patch("/api/v1/integrations/github", headers=headers, json={"enabled": False})

        updated = (
            await api.patch(
                "/api/v1/integrations/github",
                headers=headers,
                json={"repository": "hsilviu05/nova"},
            )
        ).json()

        assert updated["repository"] == "hsilviu05/nova"
        assert updated["enabled"] is False

    async def test_rotating_re_enables_a_disabled_integration(self, api: AsyncClient) -> None:
        """Asking for a new secret is asking for it to work again."""
        headers = await _register(api)
        await api.post("/api/v1/integrations/github", headers=headers, json={})
        await api.patch("/api/v1/integrations/github", headers=headers, json={"enabled": False})

        rotated = (await api.post("/api/v1/integrations/github", headers=headers, json={})).json()

        assert rotated["enabled"] is True


class TestDeduplicationWhenRedisIsDown:
    async def test_a_delivery_is_still_processed(
        self,
        settings,
        engine,
        session_factory,
        redis_client,  # type: ignore[no-untyped-def]
    ) -> None:
        """This fails open, unlike the confirmation store.

        Losing dedupe means a replayed delivery rewrites ``last_event`` with
        the same sentence it already held. Refusing every delivery until
        Redis comes back would be a much worse answer than that.
        """
        from redis.exceptions import ConnectionError as RedisConnectionError

        class HalfDeadRedis:
            """Works for sessions and rate limiting, fails on the dedupe key."""

            def __init__(self, real: Any) -> None:
                self._real = real

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

            async def set(self, key: str, *args: Any, **kwargs: Any) -> Any:
                if key.startswith("github:delivery:"):
                    raise RedisConnectionError("connection refused")
                return await self._real.set(key, *args, **kwargs)

        app = build_test_app(
            settings,
            engine=engine,
            session_factory=session_factory,
            redis=HalfDeadRedis(redis_client),  # type: ignore[arg-type]
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://nova.test"
        ) as api:
            headers = await _register(api)
            created = (
                await api.post("/api/v1/integrations/github", headers=headers, json={})
            ).json()

            response = await deliver(
                api, created["webhook_url"], created["secret"], green(), delivery="d-1"
            )

            assert response.status_code == 200
            assert response.json()["status"] == "recorded"
