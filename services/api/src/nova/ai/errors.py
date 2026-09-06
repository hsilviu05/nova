"""Provider failures, expressed in NOVA's own terms.

Adapters translate vendor exceptions into these so the conversation service
never imports a vendor SDK to catch an error -- which would defeat the
abstraction as thoroughly as calling one would.
"""

from __future__ import annotations

from nova.core.errors import NovaError


class AIProviderError(NovaError):
    """A provider failed in a way the caller cannot fix."""

    status_code = 502
    code = "ai_provider_error"
    message = "NOVA could not think of a reply just now."


class AIUnavailableError(AIProviderError):
    """The provider is unreachable, overloaded, or rate limiting us."""

    status_code = 503
    code = "ai_unavailable"
    message = "NOVA is unreachable right now. Try again shortly."


class AIConfigurationError(AIProviderError):
    """The provider is not configured -- typically a missing credential.

    A 500 rather than a 503: nothing the caller does will help, and it is an
    operator error rather than a transient one.
    """

    status_code = 500
    code = "ai_not_configured"
    message = "NOVA's AI provider is not configured."


class AIRefusalError(AIProviderError):
    """The model declined to answer.

    Distinct from a failure: the request reached the model, which chose not
    to respond. Surfaced separately so the app can say something honest
    rather than reporting a fault that did not occur.
    """

    status_code = 422
    code = "ai_refused"
    message = "NOVA would rather not answer that."
