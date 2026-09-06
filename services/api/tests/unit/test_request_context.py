"""Request-ID handling in the context middleware."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nova.middleware.errors import register_exception_handlers
from nova.middleware.request_context import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
)


def _app(*, trust_incoming_id: bool) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, trust_incoming_id=trust_incoming_id)
    register_exception_handlers(app)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("deliberate failure")

    return app


async def _get(app: FastAPI, path: str, headers: dict[str, str] | None = None):
    # raise_app_exceptions=False so the transport returns the 500 the handler
    # produced instead of re-raising it, which is what a real client sees.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://nova.test",
    ) as client:
        return await client.get(path, headers=headers)


class TestRequestIdGeneration:
    async def test_assigns_an_id(self) -> None:
        response = await _get(_app(trust_incoming_id=False), "/ping")
        # Must parse as a UUID, which is what makes it safe to log.
        uuid.UUID(response.headers[REQUEST_ID_HEADER])

    async def test_ignores_incoming_id_by_default(self) -> None:
        response = await _get(
            _app(trust_incoming_id=False),
            "/ping",
            headers={REQUEST_ID_HEADER: str(uuid.uuid4())},
        )
        uuid.UUID(response.headers[REQUEST_ID_HEADER])


class TestTrustedRequestId:
    async def test_reuses_a_valid_incoming_uuid(self) -> None:
        incoming = str(uuid.uuid4())
        response = await _get(
            _app(trust_incoming_id=True), "/ping", headers={REQUEST_ID_HEADER: incoming}
        )
        assert response.headers[REQUEST_ID_HEADER] == incoming

    @pytest.mark.parametrize("malformed", ["not-a-uuid", "", "'; DROP TABLE users; --", "a" * 500])
    async def test_rejects_a_malformed_incoming_id(self, malformed: str) -> None:
        """A non-UUID header must never reach the log fields verbatim."""
        response = await _get(
            _app(trust_incoming_id=True),
            "/ping",
            headers={REQUEST_ID_HEADER: malformed},
        )
        assert response.headers[REQUEST_ID_HEADER] != malformed
        uuid.UUID(response.headers[REQUEST_ID_HEADER])


class TestUnhandledExceptions:
    async def test_becomes_a_500_envelope(self) -> None:
        response = await _get(_app(trust_incoming_id=False), "/boom")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"

    async def test_does_not_leak_the_exception_message(self) -> None:
        response = await _get(_app(trust_incoming_id=False), "/boom")
        assert "deliberate failure" not in response.text
        assert "RuntimeError" not in response.text
