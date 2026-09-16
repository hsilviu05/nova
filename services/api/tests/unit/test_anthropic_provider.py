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
from nova.ai.base import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    StreamCompleted,
    TextDelta,
    ToolCall,
    ToolCallRequested,
    ToolDefinition,
    ToolOutcome,
)
from nova.ai.errors import (
    AIConfigurationError,
    AIProviderError,
    AIRefusalError,
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


# ---------------------------------------------------------------------------
# Streaming and completion
#
# Faked at the *SDK* boundary rather than at HTTP. The code below translates
# SDK objects into NOVA's own events, and that translation is the thing worth
# testing; faking the wire would assert that our fake matches our
# expectations, which is what the note at the top of this file rules out.
# ---------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, cache_read: int | None = 3) -> None:
        self.input_tokens = 41
        self.output_tokens = 7
        if cache_read is not None:
            self.cache_read_input_tokens = cache_read


class TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: object) -> None:
        self.id = id
        self.name = name
        self.input = input


class FakeMessage:
    def __init__(
        self,
        *,
        content: list[object] | None = None,
        stop_reason: str = "end_turn",
        cache_read: int | None = 3,
    ) -> None:
        self.content = content if content is not None else [TextBlock("A reply.")]
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = FakeUsage(cache_read)


class FakeStream:
    """Stands in for the SDK's streaming context manager."""

    def __init__(self, parts: list[str], final: FakeMessage, raises: Exception | None = None):
        self._parts = parts
        self._final = final
        self._raises = raises

    async def __aenter__(self) -> FakeStream:
        if self._raises is not None:
            raise self._raises
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self):  # type: ignore[no-untyped-def]
        async def generate():  # type: ignore[no-untyped-def]
            for part in self._parts:
                yield part

        return generate()

    async def get_final_message(self) -> FakeMessage:
        return self._final


def _with_stream(
    provider: AnthropicChatProvider,
    parts: list[str],
    final: FakeMessage,
    raises: Exception | None = None,
) -> list[dict[str, object]]:
    """Point the provider at a fake stream, recording the params it sends."""
    seen: list[dict[str, object]] = []

    def method(**params: object) -> FakeStream:
        seen.append(params)
        return FakeStream(parts, final, raises)

    provider._stream_method = lambda: method  # type: ignore[method-assign]
    return seen


class TestStreaming:
    async def test_text_arrives_then_a_completion_event(
        self, provider: AnthropicChatProvider
    ) -> None:
        _with_stream(provider, ["Hel", "lo"], FakeMessage())

        events = [e async for e in provider.stream(_request())]

        assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
        completed = events[-1]
        assert isinstance(completed, StreamCompleted)
        assert completed.stop_reason == "end_turn"
        assert completed.usage.input_tokens == 41
        assert completed.usage.cache_read_tokens == 3

    async def test_a_refusal_is_raised_rather_than_returned(
        self, provider: AnthropicChatProvider
    ) -> None:
        """A refusal arrives as a normal 200 with a stop_reason, so it is
        only visible once the stream has drained."""
        _with_stream(provider, ["I'd rather"], FakeMessage(stop_reason="refusal"))

        with pytest.raises(AIRefusalError):
            _ = [e async for e in provider.stream(_request())]

    async def test_tool_calls_follow_the_text(self, provider: AnthropicChatProvider) -> None:
        final = FakeMessage(
            content=[TextBlock("Checking."), ToolUseBlock("c1", "system_health", {"deep": True})],
            stop_reason="tool_use",
        )
        _with_stream(provider, ["Checking."], final)

        events = [e async for e in provider.stream(_request())]
        calls = [e.call for e in events if isinstance(e, ToolCallRequested)]

        assert calls[0].id == "c1"
        assert calls[0].name == "system_health"
        assert calls[0].arguments == {"deep": True}

    async def test_tool_input_that_is_not_a_mapping_becomes_empty(
        self, provider: AnthropicChatProvider
    ) -> None:
        """The SDK types it as ``object`` because a tool's schema is the
        tool's own business. Anything that is not a mapping is not a usable
        call, so it is dropped rather than passed along half-formed."""
        final = FakeMessage(content=[ToolUseBlock("c1", "wipe", "not a mapping")])
        _with_stream(provider, [], final)

        events = [e async for e in provider.stream(_request())]
        calls = [e.call for e in events if isinstance(e, ToolCallRequested)]

        assert calls[0].arguments == {}

    async def test_a_status_error_is_translated(self, provider: AnthropicChatProvider) -> None:
        _with_stream(
            provider, [], FakeMessage(), raises=_status_error(anthropic.RateLimitError, 429)
        )

        with pytest.raises(AIUnavailableError):
            _ = [e async for e in provider.stream(_request())]

    async def test_a_connection_error_is_unavailable(self, provider: AnthropicChatProvider) -> None:
        failure = anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        _with_stream(provider, [], FakeMessage(), raises=failure)

        with pytest.raises(AIUnavailableError):
            _ = [e async for e in provider.stream(_request())]


class TestComplete:
    async def test_returns_text_usage_and_stop_reason(
        self, provider: AnthropicChatProvider
    ) -> None:
        _with_stream(provider, [], FakeMessage())

        completion = await provider.complete(_request())

        assert completion.text == "A reply."
        assert completion.model == "claude-opus-5"
        assert completion.stop_reason == "end_turn"
        assert completion.usage.output_tokens == 7

    async def test_only_text_blocks_become_text(self, provider: AnthropicChatProvider) -> None:
        final = FakeMessage(
            content=[TextBlock("Part one. "), ToolUseBlock("c", "t", {}), TextBlock("Part two.")]
        )
        _with_stream(provider, [], final)

        completion = await provider.complete(_request())

        assert completion.text == "Part one. Part two."
        assert len(completion.tool_calls) == 1

    async def test_a_missing_cache_counter_reads_as_zero(
        self, provider: AnthropicChatProvider
    ) -> None:
        """Not every response carries one, and a KeyError here would fail a
        reply that had already succeeded."""
        _with_stream(provider, [], FakeMessage(cache_read=None))

        completion = await provider.complete(_request())

        assert completion.usage.cache_read_tokens == 0

    async def test_a_refusal_is_raised(self, provider: AnthropicChatProvider) -> None:
        _with_stream(provider, [], FakeMessage(stop_reason="refusal"))

        with pytest.raises(AIRefusalError):
            await provider.complete(_request())

    async def test_a_status_error_is_translated(self, provider: AnthropicChatProvider) -> None:
        _with_stream(
            provider, [], FakeMessage(), raises=_status_error(anthropic.InternalServerError, 500)
        )

        with pytest.raises(AIUnavailableError):
            await provider.complete(_request())

    async def test_a_connection_error_is_unavailable(self, provider: AnthropicChatProvider) -> None:
        failure = anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        _with_stream(provider, [], FakeMessage(), raises=failure)

        with pytest.raises(AIUnavailableError):
            await provider.complete(_request())


class TestToolDefinitions:
    def test_tools_are_sent_in_the_messages_api_shape(
        self, provider: AnthropicChatProvider
    ) -> None:
        params = provider._build_params(
            _request(
                tools=(
                    ToolDefinition(
                        name="git_status",
                        description="Repository state.",
                        input_schema={"type": "object", "properties": {}},
                    ),
                )
            )
        )

        assert params["tools"][0]["name"] == "git_status"
        assert params["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    def test_tool_results_go_in_a_user_turn_as_blocks(
        self, provider: AnthropicChatProvider
    ) -> None:
        """There is no tool role here, unlike the OpenAI-shaped formats."""
        params = provider._build_params(
            _request(
                messages=[
                    ChatMessage(
                        role="user",
                        tool_results=(
                            ToolOutcome(call_id="c1", name="git_status", content="clean"),
                        ),
                    )
                ]
            )
        )

        turn = params["messages"][0]
        assert turn["role"] == "user"
        assert turn["content"][0]["type"] == "tool_result"
        assert turn["content"][0]["tool_use_id"] == "c1"
        assert "clean" in turn["content"][0]["content"]

    def test_an_assistant_turn_carries_text_then_its_calls(
        self, provider: AnthropicChatProvider
    ) -> None:
        params = provider._build_params(
            _request(
                messages=[
                    ChatMessage(
                        role="assistant",
                        content="Checking.",
                        tool_calls=(ToolCall(id="c1", name="git_status", arguments={}),),
                    )
                ]
            )
        )

        blocks = params["messages"][0]["content"]
        assert [b["type"] for b in blocks] == ["text", "tool_use"]

    def test_an_assistant_turn_with_calls_and_no_text_omits_the_text_block(
        self, provider: AnthropicChatProvider
    ) -> None:
        params = provider._build_params(
            _request(
                messages=[
                    ChatMessage(
                        role="assistant",
                        tool_calls=(ToolCall(id="c1", name="git_status", arguments={}),),
                    )
                ]
            )
        )

        assert [b["type"] for b in params["messages"][0]["content"]] == ["tool_use"]


class TestClosing:
    async def test_closing_releases_the_sdk_client(self, provider: AnthropicChatProvider) -> None:
        closed: list[bool] = []

        async def close() -> None:
            closed.append(True)

        provider._client.close = close  # type: ignore[method-assign]
        await provider.aclose()

        assert closed == [True]


class TestTheSystemPrompt:
    def test_the_instructions_are_marked_for_caching(self, provider: AnthropicChatProvider) -> None:
        """The system prompt is long and identical on every turn.

        Marking it ephemeral is what keeps a conversation from paying for it
        again on each message.
        """
        params = provider._build_params(_request())

        system = params["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_retrieved_context_is_a_second_block_rather_than_appended(
        self, provider: AnthropicChatProvider
    ) -> None:
        """It changes with every turn.

        Concatenating it into the cached block would invalidate the cache on
        each message, which costs more than the context is worth.
        """
        params = provider._build_params(_request(context="The owner drinks coffee black."))

        system = params["system"]
        assert len(system) == 2
        assert system[1] == {"type": "text", "text": "The owner drinks coffee black."}
        assert "cache_control" not in system[1]

    def test_no_context_means_one_block(self, provider: AnthropicChatProvider) -> None:
        params = provider._build_params(_request(context=None))

        assert len(params["system"]) == 1

    def test_the_adapter_declares_tool_support(self, provider: AnthropicChatProvider) -> None:
        """What the chat loop branches on before offering the model any
        tools. An adapter that lied here would have its calls dropped."""
        assert provider.supports_tools is True
