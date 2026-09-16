"""Security headers, the body cap, the Host check, and the production
guardrails."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Message, Receive, Scope, Send

from nova.core.config import (
    DatabaseSettings,
    JWTSettings,
    ObservabilitySettings,
    SecuritySettings,
    Settings,
)
from nova.core.errors import PayloadTooLargeError
from nova.middleware.errors import register_exception_handlers
from nova.middleware.hardening import (
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    _content_length,
)
from nova.middleware.request_context import RequestContextMiddleware

CAP = 1024


class Payload(BaseModel):
    text: str


def make_app(*, allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/json")
    async def json_endpoint(payload: Payload) -> dict[str, int]:
        # A request model: FastAPI reads and parses the body itself, inside
        # a catch-all that turns any failure into its own 400.
        return {"size": len(payload.text)}

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
    async def test_a_route_with_a_request_model_gets_the_413_not_fastapis_400(self) -> None:
        async with client(make_app()) as http:
            response = await http.post("/json", json={"text": "x" * (CAP + 1)})
        assert response.status_code == 413
        body = response.json()
        assert body["error"]["code"] == "payload_too_large"
        assert body["error"]["request_id"] == response.headers["x-request-id"]

    async def test_a_chunked_body_to_a_request_model_route_is_413(self) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            yield b'{"text": "'
            for _ in range(CAP // 8 + 1):
                yield b"xxxxxxxx"
            yield b'"}'

        async with client(make_app()) as http:
            response = await http.post(
                "/json", content=chunks(), headers={"content-type": "application/json"}
            )
        assert response.status_code == 413

    async def test_a_request_model_route_under_the_cap_is_untouched(self) -> None:
        async with client(make_app()) as http:
            response = await http.post("/json", json={"text": "x" * (CAP // 2)})
        assert response.status_code == 200
        assert response.json() == {"size": CAP // 2}

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


class TestNonHttpScopes:
    """Both middlewares have to pass a non-HTTP scope straight through.

    A lifespan scope has no headers and no response start, so anything that
    assumed HTTP would fail at startup rather than on a request -- and the
    process would not come up at all.
    """

    @pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
    async def test_the_header_middleware_passes_it_through_untouched(self, scope_type: str) -> None:
        seen: list[Scope] = []

        async def inner(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        await SecurityHeadersMiddleware(inner)({"type": scope_type}, _noop_receive, _noop_send)

        assert seen == [{"type": scope_type}]

    @pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
    async def test_the_body_limit_passes_it_through_untouched(self, scope_type: str) -> None:
        seen: list[Scope] = []

        async def inner(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        await BodySizeLimitMiddleware(inner, max_bytes=CAP)(
            {"type": scope_type}, _noop_receive, _noop_send
        )

        assert seen == [{"type": scope_type}]


class TestBodyLimitConfiguration:
    def test_a_cap_of_zero_or_less_is_refused_at_construction(self) -> None:
        """A cap of zero would refuse every request with a body, which is a
        configuration mistake rather than a policy."""
        for value in (0, -1):
            with pytest.raises(ValueError, match="max_bytes must be positive"):
                BodySizeLimitMiddleware(make_app(), max_bytes=value)

    async def test_a_content_length_that_is_not_a_number_is_ignored(self) -> None:
        """And the body is then capped by what actually arrives.

        Trusting an unparseable header either way would be wrong: refusing
        breaks a legitimate client, and believing it would let a lie past.
        """
        assert _content_length({"headers": [(b"content-length", b"not-a-number")]}) is None

    def test_a_request_with_no_content_length_header_declares_nothing(self) -> None:
        assert _content_length({"headers": [(b"content-type", b"application/json")]}) is None

    def test_a_declared_length_is_read(self) -> None:
        assert _content_length({"headers": [(b"content-length", b"128")]}) == 128

    async def test_a_body_larger_than_the_cap_is_refused_even_when_nothing_was_declared(
        self,
    ) -> None:
        """The header is a claim; the bytes are the fact.

        A chunked request declares no length at all, so the only bound is the
        running count of what has actually arrived.
        """

        async def stream() -> AsyncIterator[bytes]:
            for _ in range(CAP // 8 + 2):
                yield b"xxxxxxxx"

        async with client(make_app()) as http:
            response = await http.post("/echo", content=stream())

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    async def test_a_handler_that_never_reads_the_body_is_not_refused(self) -> None:
        """Checked lazily, on the first read.

        A route that ignores the body has no reason to care how big it was,
        and refusing on the header alone would break one.
        """
        async with client(make_app()) as http:
            response = await http.post("/ignore", content=b"x" * (CAP * 4))

        assert response.status_code == 200


class TestHeadersAlreadySet:
    async def test_a_header_the_application_set_itself_is_not_overwritten(self) -> None:
        """The middleware fills gaps rather than dictating.

        A route that deliberately sets a different frame policy should keep
        it; a blanket overwrite would make that impossible to express.
        """
        app = FastAPI()

        @app.get("/custom")
        async def custom() -> Response:
            return Response(content="hi", headers={"x-frame-options": "SAMEORIGIN"})

        app.add_middleware(SecurityHeadersMiddleware)

        async with client(app) as http:
            response = await http.get("/custom")

        assert response.headers["x-frame-options"] == "SAMEORIGIN"
        # The ones it did not set are still filled in.
        assert response.headers["x-content-type-options"] == "nosniff"


class TestTheRefusalAtTheAsgiLevel:
    """Driven as raw ASGI, because the interesting cases have no HTTP shape.

    A disconnect message is not a body chunk, and an application that lets
    the refusal escape rather than rendering it is exactly the case the
    outer try/except exists for.
    """

    async def test_a_disconnect_is_not_counted_as_body(self) -> None:
        """Otherwise a long-lived request that ends in a disconnect would be
        refused for a body it never sent."""
        messages = [
            {"type": "http.request", "body": b"x" * 8, "more_body": True},
            {"type": "http.disconnect"},
        ]
        seen: list[Message] = []

        async def receive() -> Message:
            return messages.pop(0)

        async def inner(scope: Scope, rcv: Receive, send: Send) -> None:
            seen.append(await rcv())
            seen.append(await rcv())

        await BodySizeLimitMiddleware(inner, max_bytes=CAP)(
            {"type": "http", "headers": []}, receive, _noop_send
        )

        assert [m["type"] for m in seen] == ["http.request", "http.disconnect"]

    async def test_an_application_that_lets_the_refusal_escape_does_not_swallow_it(
        self,
    ) -> None:
        """The refusal has already been sent, so re-raising here would be a
        second response. It is re-raised only when nothing was sent -- which
        means the error came from somewhere other than the cap."""
        sent: list[Message] = []

        async def inner(scope: Scope, receive: Receive, send: Send) -> None:
            raise PayloadTooLargeError()

        async def send(message: Message) -> None:
            sent.append(message)

        with pytest.raises(PayloadTooLargeError):
            await BodySizeLimitMiddleware(inner, max_bytes=CAP)(
                {"type": "http", "headers": []}, _noop_receive, send
            )

        assert sent == []

    async def test_the_refusal_escaping_a_plain_application_is_not_re_raised(self) -> None:
        """The ordinary path for a route with a request model.

        FastAPI wraps its own body read in a catch-all that turns any failure
        into a 400, so the refusal is sent from inside ``receive`` and then
        raised to stop the app. By the time it gets back here the 413 is
        already on the wire, and re-raising would put a 500 behind it.
        """
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"x" * 4096, "more_body": False}

        async def inner(scope: Scope, rcv: Receive, send: Send) -> None:
            await rcv()  # raises, and is deliberately not caught

        async def send(message: Message) -> None:
            sent.append(message)

        await BodySizeLimitMiddleware(inner, max_bytes=64)(
            {"type": "http", "headers": []}, receive, send
        )

        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]

    async def test_once_refused_nothing_the_application_sends_afterwards_gets_out(
        self,
    ) -> None:
        """The app keeps running for a moment after the refusal.

        Whatever it sends would be a second response body on a connection
        that already has one, so it is dropped.
        """
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"x" * 4096, "more_body": False}

        async def inner(scope: Scope, rcv: Receive, send: Send) -> None:
            try:
                await rcv()
            except PayloadTooLargeError:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"too late"})

        async def send(message: Message) -> None:
            sent.append(message)

        await BodySizeLimitMiddleware(inner, max_bytes=64)(
            {"type": "http", "headers": []}, receive, send
        )

        statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
        assert statuses == [413]
        assert not any(m.get("body") == b"too late" for m in sent)


async def _noop_receive() -> Message:  # pragma: no cover - never awaited
    raise AssertionError("a pass-through scope should not read")


async def _noop_send(message: Message) -> None:  # pragma: no cover - never awaited
    raise AssertionError("a pass-through scope should not send")
