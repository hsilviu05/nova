"""Anthropic adapter for :class:`~nova.ai.base.ChatProvider`.

The only module in NOVA that imports the Anthropic SDK. Everything else
depends on the protocol in :mod:`nova.ai.base`, so swapping providers is a
configuration change rather than a refactor.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from nova.ai.base import ChatCompletion, ChatMessage, ChatRequest, TokenUsage
from nova.ai.errors import (
    AIConfigurationError,
    AIProviderError,
    AIRefusalError,
    AIUnavailableError,
)
from nova.core.logging import get_logger

logger = get_logger(__name__)

# Beta flag enabling server-side fallback routing. When a safety classifier
# declines a request, the server retries on another model by refusal category
# rather than returning nothing -- so a companion stays responsive instead of
# going silent on an awkward question.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicChatProvider:
    """Chat completions through the Anthropic Messages API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        enable_fallbacks: bool = True,
    ) -> None:
        if not api_key:
            raise AIConfigurationError(
                "No Anthropic API key configured.", code="ai_missing_api_key"
            )

        self._model = model
        self._enable_fallbacks = enable_fallbacks
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    # -- requests ---------------------------------------------------------

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """Yield reply text as the model produces it.

        Raises:
            AIRefusalError: if the model declined.
            AIUnavailableError: on rate limiting or an unreachable API.
            AIProviderError: on any other provider failure.
        """
        params = self._build_params(request)

        try:
            async with self._stream_method()(**params) as stream:
                async for text in stream.text_stream:
                    yield text

                final = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise self._translate(exc) from exc
        except anthropic.APIConnectionError as exc:
            raise AIUnavailableError() from exc

        # Checked after the stream drains: a refusal arrives as a normal
        # 200 with a stop_reason, not as an exception.
        if final.stop_reason == "refusal":
            logger.info("ai_refused", model=self._model)
            raise AIRefusalError()

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        """Return a whole reply.

        Streams internally regardless: a long reply on a non-streaming call
        can exceed the HTTP timeout, and the accumulated message is the same.
        """
        params = self._build_params(request)

        try:
            async with self._stream_method()(**params) as stream:
                message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise self._translate(exc) from exc
        except anthropic.APIConnectionError as exc:
            raise AIUnavailableError() from exc

        if message.stop_reason == "refusal":
            raise AIRefusalError()

        return ChatCompletion(
            text="".join(block.text for block in message.content if block.type == "text"),
            model=message.model,
            usage=TokenUsage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
                cache_read_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            ),
            stop_reason=message.stop_reason,
        )

    # -- internals --------------------------------------------------------

    def _stream_method(self) -> Any:
        """The stream callable for the namespace these parameters require.

        ``betas`` and ``fallbacks`` exist only on ``client.beta.messages`` --
        passing them to the stable namespace is a ``TypeError`` on the first
        real call, so the endpoint follows from whether fallbacks are on
        rather than being used unconditionally.

        Returns the bound method rather than an open manager so the choice
        can be asserted without starting a request.
        """
        if self._enable_fallbacks:
            return self._client.beta.messages.stream
        return self._client.messages.stream

    def _build_params(self, request: ChatRequest) -> dict[str, Any]:
        # The persona carries the cache breakpoint and is byte-stable across
        # turns; anything volatile goes in a second block *after* it, so a
        # different set of retrieved memories costs a cache miss on itself
        # rather than on the whole prefix.
        system: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": request.system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if request.context:
            system.append({"type": "text", "text": request.context})

        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": [self._as_param(m) for m in request.messages],
            # Effort tunes thinking depth and total spend. Chat replies are
            # short and latency-sensitive, so the service asks for "low".
            "output_config": {"effort": request.effort},
        }

        if self._enable_fallbacks:
            # Route around a refusal by category rather than maintaining a
            # model list of our own.
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        return params

    @staticmethod
    def _as_param(message: ChatMessage) -> dict[str, str]:
        return {"role": message.role, "content": message.content}

    @staticmethod
    def _translate(exc: anthropic.APIStatusError) -> AIProviderError:
        """Map a vendor exception onto NOVA's error hierarchy.

        Most specific first. Anything unrecognised becomes a generic
        provider error rather than escaping as a 500.
        """
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            logger.error("ai_credentials_rejected", status=exc.status_code)
            return AIConfigurationError()
        if isinstance(exc, anthropic.RateLimitError):
            logger.warning("ai_rate_limited")
            return AIUnavailableError(
                "NOVA is thinking too hard. Try again in a moment.",
                code="ai_rate_limited",
            )
        if isinstance(exc, anthropic.InternalServerError):
            logger.warning("ai_upstream_error", status=exc.status_code)
            return AIUnavailableError()

        logger.error("ai_request_failed", status=exc.status_code)
        return AIProviderError()
