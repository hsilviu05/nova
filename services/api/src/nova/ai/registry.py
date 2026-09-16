"""Provider selection.

The one place that knows which concrete adapter exists. Everything else
receives a :class:`~nova.ai.base.ChatProvider` and cannot tell which one it
got, which is the point of ADR 002.
"""

from __future__ import annotations

from nova.ai.anthropic_provider import AnthropicChatProvider
from nova.ai.base import ChatProvider, EmbeddingProvider
from nova.ai.errors import AIConfigurationError
from nova.ai.lexical_embeddings import LexicalEmbeddingProvider
from nova.ai.offline import OfflineChatProvider, OfflineEmbeddingProvider
from nova.ai.ollama_provider import OllamaChatProvider
from nova.ai.openai_compatible import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
)
from nova.core.config import AISettings
from nova.core.logging import get_logger
from nova.models.memory import EMBEDDING_DIMENSIONS

logger = get_logger(__name__)


def build_chat_provider(settings: AISettings) -> ChatProvider:
    """Construct the configured chat provider.

    Nothing here reaches the network. A local model server that is not
    running is discovered on the first request, not at startup, because a
    terminal that refuses to boot when Ollama is down is a terminal that
    cannot tell you Ollama is down. ``/api/v1/system/status`` probes it, and
    the dashboard says so.

    Falls back to the offline provider when a cloud provider is selected but
    no key is present, rather than refusing to start. The warning makes the
    degraded state obvious in the logs, and the dashboard names it.

    Raises:
        AIConfigurationError: if the configured provider is unknown, which
            is a typo rather than a missing credential.
    """
    match settings.chat_provider:
        case "offline":
            return OfflineChatProvider()

        case "ollama":
            # No degraded fallback here, unlike anthropic-without-a-key: a
            # missing credential is known at boot, but whether a local server
            # is up is a runtime fact. If it is not, every call raises
            # AIUnavailableError and the API answers 503, which is the truth
            # -- and /api/v1/system/status says so on the dashboard.
            return OllamaChatProvider(
                base_url=settings.ollama_base_url,
                model=settings.chat_model,
                timeout_seconds=settings.request_timeout_seconds,
                keep_alive=settings.ollama_keep_alive,
            )

        case "openai_compatible":
            key = settings.openai_api_key
            return OpenAICompatibleChatProvider(
                base_url=settings.openai_base_url,
                model=settings.chat_model,
                # Optional: a local llama.cpp or vLLM server usually wants no
                # credential at all, and sending an empty bearer token to one
                # that does not expect it is a 400 on every request.
                api_key=key.get_secret_value() if key else None,
                timeout_seconds=settings.request_timeout_seconds,
            )

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

    Embeddings have their own interface for a reason that outlived its
    original cause: a chat provider and an embedder are rarely the same
    vendor, and the widths they produce are incompatible. ``lexical`` is the
    default because it needs nothing installed and genuinely retrieves -- by
    shared vocabulary rather than meaning, which is a real ceiling but not a
    pretence.

    Pointing this at a local embedding server is a configuration change plus
    a re-embedding pass, and only works for a model whose width matches the
    memories column. That is a real constraint, stated rather than papered
    over by padding or truncating vectors, which would silently destroy the
    geometry retrieval depends on.

    Raises:
        AIConfigurationError: if the configured width disagrees with the
            database column. Vectors of different widths cannot be compared,
            so this has to fail loudly at startup rather than on first insert.
    """
    if settings.embedding_dimensions != EMBEDDING_DIMENSIONS:
        raise AIConfigurationError(
            f"Embedding width {settings.embedding_dimensions} does not match the "
            f"memories column ({EMBEDDING_DIMENSIONS}). Changing it needs a "
            "migration and a re-embedding pass.",
            code="ai_embedding_dimension_mismatch",
        )

    match settings.embedding_provider:
        case "hash":
            return OfflineEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)
        case "openai_compatible":
            key = settings.openai_api_key
            return OpenAICompatibleEmbeddingProvider(
                base_url=settings.openai_base_url,
                model=settings.embedding_model,
                dimensions=EMBEDDING_DIMENSIONS,
                api_key=key.get_secret_value() if key else None,
                timeout_seconds=settings.request_timeout_seconds,
            )
        case _:
            return LexicalEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)
