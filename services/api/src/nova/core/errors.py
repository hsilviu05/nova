"""Domain error hierarchy and the HTTP envelope it maps onto.

Services raise these; the API layer never constructs ``HTTPException``
directly. That keeps business logic free of transport concerns and gives
every client one error shape to parse.
"""

from __future__ import annotations

from typing import Any


class NovaError(Exception):
    """Base class for expected, client-visible failures."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)


class AuthenticationError(NovaError):
    """Credentials are missing, malformed, expired, or wrong."""

    status_code = 401
    code = "authentication_failed"
    message = "Authentication failed."


class AuthorizationError(NovaError):
    """Authenticated, but not allowed to do this."""

    status_code = 403
    code = "forbidden"
    message = "You do not have permission to perform this action."


class NotFoundError(NovaError):
    """The requested resource does not exist, or is not visible to the caller."""

    status_code = 404
    code = "not_found"
    message = "Resource not found."


class ConflictError(NovaError):
    """The request conflicts with the current state, e.g. a duplicate email."""

    status_code = 409
    code = "conflict"
    message = "The request conflicts with existing state."


class ValidationError(NovaError):
    """The request is well-formed but semantically invalid."""

    status_code = 422
    code = "validation_error"
    message = "The request payload is invalid."


class RateLimitError(NovaError):
    """Too many requests inside the configured window."""

    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Try again shortly."

    def __init__(self, retry_after_seconds: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailableError(NovaError):
    """A dependency NOVA needs is not reachable."""

    status_code = 503
    code = "service_unavailable"
    message = "A required service is unavailable."


class PayloadTooLargeError(NovaError):
    """A request body over the limit for its endpoint."""

    status_code = 413
    code = "payload_too_large"
    message = "Request body is too large."
