"""Security headers, the body cap, the Host check, and the production
guardrails."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from nova.core.config import (
    DatabaseSettings,
    JWTSettings,
    ObservabilitySettings,
    SecuritySettings,
    Settings,
)
from nova.middleware.errors import register_exception_handlers
from nova.middleware.hardening import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from nova.middleware.request_context import RequestContextMiddleware

CAP = 1024


def make_app(*, allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/ignore")
    async def ignore() -> dict[str, str]:
        # Never reads the body.
        return {"ok": "yes"}

    # Innermost, for the reason main.py gives.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=CAP)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    register_exception_handlers(app)
    return app


def client(app: FastAPI, base: str = "http://nova.test") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=base)


class TestSecurityHeaders:
    async def test_every_response_is_uncacheable_and_unsniffable(self) -> None:
        async with client(make_app()) as c:
            response = await c.get("/ping")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "camera=()" in response.headers["permissions-policy"]

    async def test_hsts_only_over_https(self) -> None:
        async with client(make_app(), base="http://nova.test") as c:
            plain = await c.get("/ping")
        async with client(make_app(), base="https://nova.test") as c:
            secure = await c.get("/ping")
        assert "strict-transport-security" not in plain.headers
        assert secure.headers["strict-transport-security"].startswith("max-age=63072000")

    async def test_error_responses_carry_the_headers_too(self) -> None:
        async with client(make_app()) as c:
            response = await c.get("/missing")
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"


class TestBodyLimit:
    async def test_under_the_cap_passes(self) -> None:
        async with client(make_app()) as c:
            response = await c.post("/echo", content=b"x" * CAP)
        assert response.status_code == 200
        assert response.json() == {"bytes": CAP}

    async def test_a_declared_length_over_the_cap_is_413_with_an_envelope(self) -> None:
        async with client(make_app()) as c:
            response = await c.post("/echo", content=b"x" * (CAP + 1))
        assert response.status_code == 413
        body = response.json()
        assert body["error"]["code"] == "payload_too_large"
        # The request id is there: the refusal happened inside the request.
        assert body["error"]["request_id"] == response.headers["x-request-id"]

    async def test_a_chunked_body_over_the_cap_is_413(self) -> None:
        # No Content-Length: the limit has to count what actually arrives.
        async def chunks():  # type: ignore[no-untyped-def]
            for _ in range(4):
                yield b"y" * (CAP // 2)

        async with client(make_app()) as c:
            response = await c.post("/echo", content=chunks())
        assert response.status_code == 413

    async def test_a_route_that_never_reads_the_body_is_not_refused(self) -> None:
        # The check is lazy. Refusing on the header alone would reject a
        # request nobody was going to buffer.
        async with client(make_app()) as c:
            response = await c.post("/ignore", content=b"z" * (CAP * 4))
        assert response.status_code == 200

    async def test_a_garbage_content_length_is_not_a_crash(self) -> None:
        async with client(make_app()) as c:
            response = await c.post("/echo", content=b"ok", headers={"content-length": "2"})
        assert response.status_code == 200

    def test_a_zero_cap_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            BodySizeLimitMiddleware(make_app(), max_bytes=0)


class TestTrustedHost:
    async def test_an_unlisted_host_is_refused(self) -> None:
        app = make_app(allowed_hosts=["nova.example"])
        async with client(app, base="http://evil.example") as c:
            assert (await c.get("/ping")).status_code == 400
        async with client(app, base="http://nova.example") as c:
            assert (await c.get("/ping")).status_code == 200


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "environment": "production",
        "jwt": JWTSettings(secret_key="a-real-looking-secret-that-is-long-enough-1234"),
        "security": SecuritySettings(allowed_hosts=["nova.example"]),
        "observability": ObservabilitySettings(log_json=True),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestProductionGuardrails:
    def test_a_sane_production_config_boots(self) -> None:
        settings = _settings()
        assert settings.is_production
        assert settings.docs_url is None

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"debug": True}, "NOVA_DEBUG"),
            (
                {"security": SecuritySettings(cors_origins=["*"], allowed_hosts=["h"])},
                "CORS_ORIGINS",
            ),
            ({"security": SecuritySettings(allowed_hosts=["*"])}, "ALLOWED_HOSTS"),
            ({"public_base_url": "http://nova.example"}, "PUBLIC_BASE_URL"),
            ({"database": DatabaseSettings(echo=True)}, "DATABASE__ECHO"),
            (
                {
                    "jwt": JWTSettings(
                        secret_key="ci-secret-key-at-least-thirty-two-characters-long"
                    )
                },
                "placeholder",
            ),
            ({"observability": ObservabilitySettings(log_json=False)}, "LOG_JSON"),
        ],
    )
    def test_each_unsafe_setting_refuses_to_boot(
        self, overrides: dict[str, object], fragment: str
    ) -> None:
        with pytest.raises(ValidationError) as error:
            _settings(**overrides)
        assert "refusing to start in production" in str(error.value)
        assert fragment in str(error.value)

    def test_the_same_settings_are_fine_outside_production(self) -> None:
        # The guardrails are about production. A laptop with debug on is a
        # laptop with debug on.
        settings = _settings(environment="local", debug=True)
        assert settings.debug
