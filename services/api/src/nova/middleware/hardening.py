"""Two ASGI middlewares that every production API needs and few have.

Both are pure ASGI rather than Starlette's ``BaseHTTPMiddleware``: that
class buffers responses and breaks streaming, and the chat endpoint streams.

**Security headers.** ``Cache-Control: no-store`` on every API response,
because every response here is personal data and a shared cache must not
keep it. ``X-Content-Type-Options: nosniff`` so a JSON body is never
sniffed into something executable. ``X-Frame-Options: DENY`` and a
``Referrer-Policy`` because they cost nothing. ``Strict-Transport-Security``
only when the request arrived over HTTPS -- the scheme uvicorn sees, which
behind a proxy is whatever ``X-Forwarded-Proto`` said once proxy headers are
trusted. Sending HSTS over plain HTTP is ignored by browsers and confuses
local development, so it is not sent there.

**Body size limit.** Nothing in FastAPI or uvicorn caps a request body by
default: a client can POST a gigabyte to ``/auth/register`` and the JSON
parser will try to hold it. The limit is enforced by wrapping ``receive``,
so a route that never reads the body never pays for the check, and a route
that does gets :class:`PayloadTooLargeError` from the same place it would
get the bytes -- which the error handlers turn into the standard 413
envelope with a request id. The webhook keeps its own, tighter cap.

This middleware must be the innermost one. Raised from inside the endpoint's
read, the exception reaches the handlers; raised one layer further out, it
would be re-raised by Starlette's ``BaseHTTPMiddleware`` from its own body
streaming task and never become a response at all. ``main.py`` adds it first
for that reason, and the tests build their app the same way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from nova.core.errors import PayloadTooLargeError

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# One mebibyte. The largest legitimate body is a telemetry page or a chat
# message; both are kilobytes. Anything near this is not a client.
DEFAULT_MAX_BODY_BYTES = 1024 * 1024

_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)
# Two years, subdomains included, once a deployment has committed to HTTPS.
_HSTS = (b"strict-transport-security", b"max-age=63072000; includeSubDomains")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        secure = scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name for name, _ in headers}
                for name, value in _STATIC_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                if secure and _HSTS[0] not in present:
                    headers.append(_HSTS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        received = 0

        async def limited_receive() -> Message:
            # Checked lazily, on the first read, so a request nobody reads the
            # body of is not refused for a header alone -- and so the error
            # surfaces inside the request, where the envelope has a request id.
            if declared is not None and declared > self.max_bytes:
                raise PayloadTooLargeError()
            message = await receive()
            if message["type"] == "http.request":
                nonlocal received
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise PayloadTooLargeError()
            return message

        await self.app(scope, limited_receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
