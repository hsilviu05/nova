"""AI provider abstraction.

Business logic imports from here, never from a vendor SDK. See ADR 002.
"""

from nova.ai.base import (
    ChatCompletion,
    ChatMessage,
    ChatProvider,
    ChatRequest,
    EmbeddingProvider,
    TokenUsage,
)
from nova.ai.errors import (
    AIConfigurationError,
    AIProviderError,
    AIRefusalError,
    AIUnavailableError,
)
from nova.ai.registry import build_chat_provider, build_embedding_provider

__all__ = [
    "AIConfigurationError",
    "AIProviderError",
    "AIRefusalError",
    "AIUnavailableError",
    "ChatCompletion",
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "EmbeddingProvider",
    "TokenUsage",
    "build_chat_provider",
    "build_embedding_provider",
]
