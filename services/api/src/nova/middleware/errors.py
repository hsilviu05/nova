"""Exception handlers.

Every failure leaves the API in the same envelope, always carrying the request
ID so a user-reported error can be found in the logs.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nova.core.errors import NovaError, RateLimitError
from nova.core.logging import get_logger
from nova.middleware.request_context import REQUEST_ID_HEADER
from nova.schemas.common import ErrorDetail, ErrorResponse

logger = get_logger(__name__)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=request_id,
            details=details or {},
        )
    )
    response_headers = dict(headers or {})
    if request_id:
        response_headers[REQUEST_ID_HEADER] = request_id

    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=response_headers,
    )


def _serialisable_validation_errors(
    exc: RequestValidationError,
) -> list[dict[str, object]]:
    """Reduce Pydantic's error list to JSON-safe, non-disclosing fields.

    Two problems with returning ``exc.errors()`` directly:

    * ``ctx`` holds the original exception object for custom validators, which
      is not JSON-serialisable -- serialising it turns a 422 into a 500.
    * ``input`` echoes the submitted value, so a failing password rule would
      reflect the password straight back to the caller (and into any log or
      proxy that records response bodies).

    Only the field location, the machine-readable type, and the message
    survive.
    """
    return [
        {
            "type": str(error.get("type", "invalid")),
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "Invalid value.")),
        }
        for error in exc.errors()
    ]


def register_exception_handlers(app: FastAPI) -> None:
    """Attach NOVA's handlers to ``app``."""

    @app.exception_handler(NovaError)
    async def _handle_nova_error(request: Request, exc: NovaError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        # 401 without a challenge header is a protocol violation.
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"

        return _envelope(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _envelope(
            request,
            status_code=422,
            code="validation_error",
            message="The request payload is invalid.",
            details={"errors": _serialisable_validation_errors(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _envelope(
            request,
            status_code=exc.status_code,
            code=f"http_{exc.status_code}",
            message=str(exc.detail),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Log the detail, return none of it: internal messages can disclose
        # schema names, file paths, or credentials.
        logger.exception("unhandled_exception", exc_type=type(exc).__name__)
        return _envelope(
            request,
            status_code=500,
            code="internal_error",
            message="An unexpected error occurred.",
        )
