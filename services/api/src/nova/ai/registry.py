"""Provider selection.

The one place that knows which concrete adapter exists. Everything else
receives a :class:`~nova.ai.base.ChatProvider` and cannot tell which one it
got, which is the point of ADR 002.
"""

from __future__ import annotations

from nova.ai.anthropic_provider import AnthropicChatProvider
from nova.ai.base import ChatProvider, EmbeddingProvider
from nova.ai.errors import AIConfigurationError
from nova.ai.offline import OfflineChatProvider, OfflineEmbeddingProvider
from nova.core.config import AISettings
from nova.core.logging import get_logger

logger = get_logger(__name__)


def build_chat_provider(settings: AISettings) -> ChatProvider:
    """Construct the configured chat provider.

    Falls back to the offline provider when ``anthropic`` is selected but no
    key is present, rather than refusing to start. A companion that answers
    honestly with a canned line is more useful than one that will not boot,
    and the warning makes the degraded state obvious in the logs.

    Raises:
        AIConfigurationError: if the configured provider is unknown, which
            is a typo rather than a missing credential.
    """
    match settings.chat_provider:
        case "offline":
            return OfflineChatProvider()

        case "anthropic":
            key = settings.anthropic_api_key
            if key is None or not key.get_secret_value():
                logger.warning(
                    "ai_provider_degraded",
                    requested="anthropic",
                    using="offline",
                    reason="no_api_key",
                )
                return OfflineChatProvider()

            return AnthropicChatProvider(
                api_key=key.get_secret_value(),
                model=settings.chat_model,
                timeout_seconds=settings.request_timeout_seconds,
                enable_fallbacks=settings.enable_refusal_fallbacks,
            )

        case unknown:  # pragma: no cover - guarded by the settings Literal
            raise AIConfigurationError(
                f"Unknown chat provider: {unknown}", code="ai_unknown_provider"
            )


def build_embedding_provider(settings: AISettings) -> EmbeddingProvider:
    """Construct the configured embedding provider.

    Only the offline implementation exists in Phase 4. Phase 5 adds a real
    one; the interface is already here so nothing has to change around it.
    """
    return OfflineEmbeddingProvider(dimensions=settings.embedding_dimensions)
