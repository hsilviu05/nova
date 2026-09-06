"""The Anthropic adapter's logic, without a network call.

Request assembly and error translation are pure and are where a mistake
would be silent. What is deliberately not tested here is the HTTP exchange
itself: faking it would assert that our fake matches our expectations, which
proves nothing about the real API.
"""

from __future__ import annotations

import anthropic
import httpx2
import pytest

from nova.ai.anthropic_provider import FALLBACK_BETA, AnthropicChatProvider
from nova.ai.base import ChatMessage, ChatProvider, ChatRequest
from nova.ai.errors import (
    AIConfigurationError,
    AIProviderError,
    AIUnavailableError,
)


@pytest.fixture
def provider() -> AnthropicChatProvider:
    return AnthropicChatProvider(api_key="sk-ant-not-a-real-key", model="claude-opus-5")


def _request(**overrides: object) -> ChatRequest:
    defaults: dict[str, object] = {
        "system": "You are NOVA.",
        "messages": [ChatMessage(role="user", content="Hello")],
    }
    return ChatRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


def _status_error(cls: type, status: int) -> anthropic.APIStatusError:
    """Build a real SDK exception, so the mapping is tested against the
    actual class hierarchy rather than a stand-in."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, json={"error": {}})
    return cls(message="boom", response=response, body=None)


class TestConstruction:
    def test_satisfies_the_protocol(self, provider: AnthropicChatProvider) -> None:
        assert isinstance(provider, ChatProvider)

    def test_reports_its_identity(self, provider: AnthropicChatProvider) -> None:
        # Stored on every assistant message, so a reply can be traced back.
        assert provider.name == "anthropic"
        assert provider.model == "claude-opus-5"

    def test_refuses_to_build_without_a_key(self) -> None:
        # Better a loud failure at construction than an opaque 401 later.
        with pytest.raises(AIConfigurationError):
            AnthropicChatProvider(api_key="", model="claude-opus-5")


class TestRequestAssembly:
    def test_sends_the_model_and_token_cap(self, provider: AnthropicChatProvider) -> None:
        params = provider._build_params(_request(max_tokens=512))

        assert params["model"] == "claude-opus-5"
        assert params["max_tokens"] == 512

    def test_system_prompt_is_marked_cacheable(self, provider: AnthropicChatProvider) -> None:
        """The system prompt is byte-stable across turns, so caching it is
        the whole reason for keeping volatile context out of it."""
        system = params_system(provider)

        assert system[0]["text"] == "You are NOVA."
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_messages_are_sent_as_role_content_pairs(self, provider: AnthropicChatProvider) -> None:
        params = provider._build_params(
            _request(
                messages=[
                    ChatMessage(role="user", content="Hi"),
                    ChatMessage(role="assistant", content="Hello"),
                    ChatMessage(role="user", content="Still there?"),
                ]
            )
        )

        assert params["messages"] == [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Still there?"},
        ]

    def test_effort_is_nested_in_output_config(self, provider: AnthropicChatProvider) -> None:
        # Top-level `effort` is not a parameter; it lives inside output_config.
        params = provider._build_params(_request(effort="low"))
        assert params["output_config"] == {"effort": "low"}

    def test_sends_no_deprecated_thinking_budget(self, provider: AnthropicChatProvider) -> None:
        # budget_tokens is rejected outright on this model generation.
        params = provider._build_params(_request())
        assert "budget_tokens" not in params
        assert "thinking" not in params

    def test_fallbacks_are_enabled_by_default(self, provider: AnthropicChatProvider) -> None:
        """A refusal routes to another model rather than returning nothing.

        A companion that goes silent on an awkward question reads as broken.
        """
        params = provider._build_params(_request())

        assert params["betas"] == [FALLBACK_BETA]
        assert params["fallbacks"] == "default"

    def test_fallbacks_can_be_disabled(self) -> None:
        provider = AnthropicChatProvider(
            api_key="sk-ant-not-a-real-key",
            model="claude-opus-5",
            enable_fallbacks=False,
        )
        params = provider._build_params(_request())

        assert "betas" not in params
        assert "fallbacks" not in params


class TestNamespaceSelection:
    def test_fallbacks_use_the_beta_namespace(self, provider: AnthropicChatProvider) -> None:
        """``betas`` and ``fallbacks`` exist only on ``beta.messages``.

        Passing them to the stable namespace is a TypeError on the first
        real call, which is exactly the kind of failure that survives to
        production when it is only reachable with a live key.
        """
        # The bound method, not an opened stream: asserting on the choice
        # must not start a request.
        assert provider._stream_method().__self__ is provider._client.beta.messages

    def test_without_fallbacks_the_stable_namespace_accepts_the_params(self) -> None:
        provider = AnthropicChatProvider(
            api_key="sk-ant-not-a-real-key",
            model="claude-opus-5",
            enable_fallbacks=False,
        )
        assert provider._stream_method().__self__ is provider._client.messages


class TestErrorTranslation:
    def test_rate_limiting_becomes_unavailable(self, provider: AnthropicChatProvider) -> None:
        error = provider._translate(_status_error(anthropic.RateLimitError, 429))

        assert isinstance(error, AIUnavailableError)
        assert error.code == "ai_rate_limited"

    @pytest.mark.parametrize(
        "cls", [anthropic.AuthenticationError, anthropic.PermissionDeniedError]
    )
    def test_rejected_credentials_become_a_configuration_error(
        self, provider: AnthropicChatProvider, cls: type
    ) -> None:
        # An operator problem, not a transient one, so it is not a 503 the
        # client should retry against.
        error = provider._translate(_status_error(cls, 401))
        assert isinstance(error, AIConfigurationError)

    def test_an_upstream_fault_becomes_unavailable(self, provider: AnthropicChatProvider) -> None:
        error = provider._translate(_status_error(anthropic.InternalServerError, 500))
        assert isinstance(error, AIUnavailableError)

    def test_anything_else_becomes_a_generic_provider_error(
        self, provider: AnthropicChatProvider
    ) -> None:
        # Never escapes as an unhandled 500.
        error = provider._translate(_status_error(anthropic.BadRequestError, 400))

        assert isinstance(error, AIProviderError)
        assert error.status_code == 502

    def test_no_vendor_type_escapes(self, provider: AnthropicChatProvider) -> None:
        """The abstraction holds only if callers never see an SDK exception."""
        error = provider._translate(_status_error(anthropic.NotFoundError, 404))
        assert not isinstance(error, anthropic.AnthropicError)


def params_system(provider: AnthropicChatProvider) -> list[dict[str, object]]:
    system = provider._build_params(_request())["system"]
    assert isinstance(system, list)
    return system
