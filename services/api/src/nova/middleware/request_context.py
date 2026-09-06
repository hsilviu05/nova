"""Request correlation and access logging.

Assigns every request an ID, binds it to the structlog context so any log line
emitted while handling that request carries it, echoes it back in
``X-Request-ID``, and records latency.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from nova.core.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"

logger = get_logger("nova.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request ID and logs one structured line per request."""

    def __init__(self, app: ASGIApp, *, trust_incoming_id: bool = False) -> None:
        super().__init__(app)
        # Off by default: an attacker-supplied ID would let a client forge
        # correlation between unrelated requests in the logs. Enable only
        # behind a gateway that sets the header itself.
        self._trust_incoming_id = trust_incoming_id

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = self._resolve_request_id(request)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response

    def _resolve_request_id(self, request: Request) -> str:
        if self._trust_incoming_id:
            incoming = request.headers.get(REQUEST_ID_HEADER)
            # Only accept a well-formed UUID; anything else is discarded so a
            # client cannot inject arbitrary text into log fields.
            if incoming:
                try:
                    return str(uuid.UUID(incoming))
                except ValueError:
                    pass
        return str(uuid.uuid4())
