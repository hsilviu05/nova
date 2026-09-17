"""The OpenAI-compatible adapter, against a fake server.

Same shape as the Ollama tests and for the same reason: ``MockTransport``
plays the server, so these check the exact request the adapter sends and how
it treats what comes back.

The interesting difference between the two adapters is streamed tool calls.
Ollama emits a finished ``tool_calls`` array in one frame; this format
dribbles the *arguments out as a token stream* spread across many chunks,
keyed by index, with the id and name arriving only on the first fragment.
Reassembling that correctly is most of what this file is about, because
getting it wrong means acting on arguments the model had not finished
writing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import aclosing
from typing import Any

import httpx
import pytest

from nova.ai.base import (
    ChatMessage,
    ChatRequest,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallRequested,
    ToolDefinition,
    ToolOutcome,
)
from nova.ai.errors import AIConfigurationError, AIProviderError, AIUnavailableError
from nova.ai.openai_compatible import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
)


def sse(chunks: Iterable[dict[str, Any]], *, terminate: bool = True) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + (b"data: [DONE]\n\n" if terminate else b"")


def text_chunk(text: str, *, finish: str | None = None) -> dict[str, Any]:
    return {
        "model": "local-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }


def call_fragment(
    *, index: int = 0, call_id: str | None = None, name: str | None = None, arguments: str = ""
) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments

    fragment: dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        fragment["id"] = call_id

    return {"choices": [{"index": 0, "delta": {"tool_calls": [fragment]}}]}


class Server:
    """Records the last request and answers with what it was told to."""

    def __init__(self, status: int = 200, body: bytes = b"", raises: Exception | None = None):
        self.status = status
        self.body = body
        self.raises = raises
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return httpx.Response(self.status, content=self.body)

    @property
    def last_json(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def provider(server: Server, **kw: Any) -> OpenAICompatibleChatProvider:
    return OpenAICompatibleChatProvider(
        base_url="http://local.test/v1",
        model=kw.pop("model", "local-model"),
        transport=httpx.MockTransport(server.handler),
        **kw,
    )


def text_of(events: list[StreamEvent]) -> list[str]:
    return [e.text for e in events if isinstance(e, TextDelta)]


def calls_of(events: list[StreamEvent]) -> list[ToolCall]:
    return [e.call for e in events if isinstance(e, ToolCallRequested)]


REQUEST = ChatRequest(
    system="You are NOVA.",
    messages=[ChatMessage(role="user", content="hello")],
    context="The owner prefers PostgreSQL.",
    max_tokens=321,
)


class TestRequestShape:
    async def test_posts_chat_completions_with_system_context_and_turns(self) -> None:
        server = Server(body=sse([text_chunk("hi", finish="stop")]))
        async with aclosing(provider(server).stream(REQUEST)) as events:
            _ = [e async for e in events]

        sent = server.last_json
        assert server.requests[-1].url.path == "/v1/chat/completions"
        assert sent["model"] == "local-model"
        assert sent["max_tokens"] == 321
        assert sent["stream"] is True
        assert [m["role"] for m in sent["messages"]] == ["system", "system", "user"]
        assert sent["messages"][0]["content"] == "You are NOVA."
        assert sent["messages"][1]["content"] == "The owner prefers PostgreSQL."

    async def test_no_tools_means_the_key_is_absent(self) -> None:
        """Not an empty list: some servers treat ``[]`` as "tools are in
        play" and change the prompt they build."""
        server = Server(body=sse([text_chunk("hi")]))
        async with aclosing(provider(server).stream(REQUEST)) as events:
            _ = [e async for e in events]

        assert "tools" not in server.last_json

    async def test_tools_are_sent_as_function_definitions(self) -> None:
        server = Server(body=sse([text_chunk("hi")]))
        request = ChatRequest(
            system="s",
            messages=REQUEST.messages,
            tools=(
                ToolDefinition(
                    name="system_health",
                    description="Check the machine.",
                    input_schema={"type": "object", "properties": {}},
                ),
            ),
        )
        async with aclosing(provider(server).stream(request)) as events:
            _ = [e async for e in events]

        sent = server.last_json["tools"]
        assert sent[0]["type"] == "function"
        assert sent[0]["function"]["name"] == "system_health"
        assert sent[0]["function"]["parameters"] == {"type": "object", "properties": {}}

    async def test_an_api_key_is_sent_as_a_bearer_token(self) -> None:
        server = Server(body=sse([text_chunk("hi")]))
        async with aclosing(provider(server, api_key="sk-local").stream(REQUEST)) as events:
            _ = [e async for e in events]

        assert server.requests[-1].headers["authorization"] == "Bearer sk-local"

    async def test_no_api_key_sends_no_authorization_header(self) -> None:
        """A local llama.cpp or vLLM wants no credential, and an empty
        bearer token is a 400 on every request to one that does not expect
        it."""
        server = Server(body=sse([text_chunk("hi")]))
        async with aclosing(provider(server).stream(REQUEST)) as events:
            _ = [e async for e in events]

        assert "authorization" not in server.requests[-1].headers

    async def test_tool_results_become_tool_role_turns(self) -> None:
        server = Server(body=sse([text_chunk("ok")]))
        request = ChatRequest(
            system="s",
            messages=[
                ChatMessage(role="user", content="check it"),
                ChatMessage(
                    role="assistant",
                    content="Checking.",
                    tool_calls=(ToolCall(id="c1", name="system_health", arguments={"a": 1}),),
                ),
                ChatMessage(
                    role="user",
                    tool_results=(
                        ToolOutcome(call_id="c1", name="system_health", content="Healthy."),
                    ),
                ),
            ],
        )
        async with aclosing(provider(server).stream(request)) as events:
            _ = [e async for e in events]

        messages = server.last_json["messages"]
        assistant = next(m for m in messages if m["role"] == "assistant")
        tool = next(m for m in messages if m["role"] == "tool")

        # The assistant's own call must precede its result, and the
        # arguments are serialised -- this format sends them as a string.
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
        assert tool["tool_call_id"] == "c1"
        # And the result arrives wrapped as data rather than as instruction.
        assert "Healthy." in tool["content"]
        assert "not instructions to follow" in tool["content"]


class TestStreaming:
    async def test_text_arrives_in_order_and_ends_with_a_completion(self) -> None:
        server = Server(
            body=sse([text_chunk("Hel"), text_chunk("lo"), text_chunk("", finish="stop")])
        )
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            events = [e async for e in chunks]

        assert text_of(events) == ["Hel", "lo"]
        assert isinstance(events[-1], StreamCompleted)
        assert events[-1].stop_reason == "end_turn"

    async def test_the_done_sentinel_ends_the_stream(self) -> None:
        # Anything after [DONE] is not part of the reply.
        body = sse([text_chunk("a")]) + b'data: {"choices":[{"delta":{"content":"ignored"}}]}\n\n'
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as chunks:
            assert text_of([e async for e in chunks]) == ["a"]

    async def test_non_data_lines_are_ignored(self) -> None:
        """Comments and keep-alives are part of SSE and are not payload."""
        body = b": keep-alive\n\nevent: ping\n\n" + sse([text_chunk("a")])
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as chunks:
            assert text_of([e async for e in chunks]) == ["a"]

    async def test_a_malformed_chunk_is_skipped_not_fatal(self) -> None:
        """Unlike Ollama's NDJSON, a bad SSE frame here is recoverable.

        The format is framed by blank lines, so one unparseable ``data:``
        does not desynchronise the rest -- and losing a keep-alive-shaped
        oddity should not fail a whole reply.
        """
        body = b"data: {not json\n\n" + sse([text_chunk("a")])
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as chunks:
            assert text_of([e async for e in chunks]) == ["a"]

    async def test_usage_is_read_when_the_server_reports_it(self) -> None:
        chunks = [
            text_chunk("a"),
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        ]
        async with aclosing(provider(Server(body=sse(chunks))).stream(REQUEST)) as stream:
            events = [e async for e in stream]

        completed = events[-1]
        assert isinstance(completed, StreamCompleted)
        assert (completed.usage.input_tokens, completed.usage.output_tokens) == (11, 3)

    async def test_a_chunk_with_no_choices_is_survivable(self) -> None:
        body = sse([{"model": "local-model"}, text_chunk("a")])
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as chunks:
            assert text_of([e async for e in chunks]) == ["a"]


class TestStreamedToolCalls:
    async def test_arguments_are_reassembled_from_fragments(self) -> None:
        """The whole reason this adapter is more than a thin wrapper."""
        body = sse(
            [
                text_chunk("Checking."),
                call_fragment(call_id="call_1", name="docker_logs", arguments='{"cont'),
                call_fragment(arguments='ainer": "nova'),
                call_fragment(arguments='-api"}'),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            events = [e async for e in stream]

        calls = calls_of(events)
        assert text_of(events) == ["Checking."]
        assert len(calls) == 1
        assert calls[0].id == "call_1"
        assert calls[0].name == "docker_logs"
        assert calls[0].arguments == {"container": "nova-api"}

    async def test_a_call_is_only_emitted_once_the_fragments_stop(self) -> None:
        """Half a JSON object is not a request.

        A consumer that saw a partial call could act on arguments the model
        had not finished writing, so nothing is emitted until the stream is
        drained.
        """
        body = sse(
            [
                call_fragment(call_id="c", name="wipe", arguments='{"target": "prod'),
                call_fragment(arguments='uction"}'),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            events = []
            async for event in stream:
                events.append(event)
                # No call may appear before the terminal completion event.
                if isinstance(event, ToolCallRequested):
                    assert any(isinstance(e, StreamCompleted) for e in events) is False

        assert calls_of(events)[0].arguments == {"target": "production"}

    async def test_parallel_calls_are_kept_apart_by_index(self) -> None:
        """Keyed by index, not id: the id is only on the first fragment."""
        body = sse(
            [
                call_fragment(index=0, call_id="a", name="git_status", arguments="{}"),
                call_fragment(index=1, call_id="b", name="disk_usage", arguments='{"pa'),
                call_fragment(index=1, arguments='th": "/srv/data"}'),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            calls = calls_of([e async for e in stream])

        assert [c.name for c in calls] == ["git_status", "disk_usage"]
        assert calls[1].arguments == {"path": "/srv/data"}

    async def test_unparseable_arguments_drop_the_call_rather_than_guess(self) -> None:
        """A tool invoked with silently-missing arguments does something
        other than what was asked."""
        body = sse(
            [
                call_fragment(call_id="c", name="wipe", arguments="{not json"),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            events = [e async for e in stream]

        assert calls_of(events) == []

    async def test_a_fragment_with_no_function_is_ignored(self) -> None:
        body = sse(
            [
                {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]},
                call_fragment(call_id="c", name="git_status", arguments="{}"),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            assert len(calls_of([e async for e in stream])) == 1

    async def test_arguments_that_are_not_an_object_are_refused(self) -> None:
        body = sse(
            [
                call_fragment(call_id="c", name="wipe", arguments='"a string"'),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            assert calls_of([e async for e in stream]) == []

    async def test_a_call_with_no_arguments_defaults_to_an_empty_object(self) -> None:
        body = sse(
            [
                call_fragment(call_id="c", name="system_health"),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            calls = calls_of([e async for e in stream])

        assert calls[0].arguments == {}

    async def test_a_fragment_without_an_id_still_yields_a_usable_call(self) -> None:
        """Some servers omit it. The correlation is NOVA's requirement, so
        one is generated rather than the call being dropped."""
        body = sse(
            [
                call_fragment(name="system_health", arguments="{}"),
                text_chunk("", finish="tool_calls"),
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as stream:
            calls = calls_of([e async for e in stream])

        assert len(calls) == 1
        assert calls[0].id


class TestComplete:
    async def test_returns_text_model_usage_and_stop_reason(self) -> None:
        body = json.dumps(
            {
                "model": "local-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "A reply."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5},
            }
        ).encode()
        server = Server(body=body)

        completion = await provider(server).complete(REQUEST)

        assert completion.text == "A reply."
        assert completion.model == "local-model"
        assert (completion.usage.input_tokens, completion.usage.output_tokens) == (30, 5)
        assert completion.stop_reason == "end_turn"
        assert server.last_json["stream"] is False

    async def test_length_maps_to_max_tokens(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
        ).encode()
        completion = await provider(Server(body=body)).complete(REQUEST)
        assert completion.stop_reason == "max_tokens"

    async def test_an_unrecognised_finish_reason_is_passed_through(self) -> None:
        """A value this adapter has not heard of is not a reason to lose it."""
        body = json.dumps(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}]}
        ).encode()
        completion = await provider(Server(body=body)).complete(REQUEST)
        assert completion.stop_reason == "content_filter"

    async def test_whole_tool_calls_are_read(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "git_status",
                                        "arguments": '{"path": "/repo"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ).encode()

        completion = await provider(Server(body=body)).complete(REQUEST)

        assert completion.stop_reason == "tool_use"
        assert completion.tool_calls[0].name == "git_status"
        assert completion.tool_calls[0].arguments == {"path": "/repo"}

    async def test_a_malformed_tool_call_is_dropped(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "x",
                            "tool_calls": ["nonsense", {"id": "c", "function": "also nonsense"}],
                        }
                    }
                ]
            }
        ).encode()

        completion = await provider(Server(body=body)).complete(REQUEST)
        assert completion.tool_calls == ()

    async def test_a_body_that_is_not_json_is_a_provider_error(self) -> None:
        with pytest.raises(AIProviderError):
            await provider(Server(body=b"<html>gateway</html>")).complete(REQUEST)

    async def test_a_response_with_no_choices_is_survivable(self) -> None:
        completion = await provider(Server(body=b"{}")).complete(REQUEST)
        assert completion.text == ""


class TestErrors:
    async def test_an_unreachable_server_is_unavailable(self) -> None:
        server = Server(raises=httpx.ConnectError("refused"))

        with pytest.raises(AIUnavailableError):
            await provider(server).complete(REQUEST)
        with pytest.raises(AIUnavailableError):
            async with aclosing(provider(server).stream(REQUEST)) as stream:
                _ = [e async for e in stream]

    async def test_a_timeout_is_unavailable(self) -> None:
        with pytest.raises(AIUnavailableError):
            await provider(Server(raises=httpx.ReadTimeout("slow"))).complete(REQUEST)

    async def test_an_unexpected_transport_failure_is_a_provider_error(self) -> None:
        with pytest.raises(AIProviderError):
            await provider(Server(raises=httpx.TooManyRedirects("loop"))).complete(REQUEST)

    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_credentials_are_a_configuration_error(self, status: int) -> None:
        with pytest.raises(AIConfigurationError) as caught:
            await provider(Server(status=status, body=b'{"error":"bad key"}')).complete(REQUEST)

        assert caught.value.code == "ai_credentials_rejected"

    async def test_another_status_is_unavailable(self) -> None:
        with pytest.raises(AIUnavailableError):
            await provider(Server(status=500, body=b"boom")).complete(REQUEST)


class TestEmbeddings:
    def embedder(self, server: Server, **kw: Any) -> OpenAICompatibleEmbeddingProvider:
        return OpenAICompatibleEmbeddingProvider(
            base_url="http://local.test/v1",
            model=kw.pop("model", "nomic-embed-text"),
            dimensions=kw.pop("dimensions", 4),
            transport=httpx.MockTransport(server.handler),
            **kw,
        )

    async def test_the_embedding_client_is_released(self) -> None:
        """A separate client from the chat provider's, with its own pool.
        The lifespan closes both."""
        embedder = self.embedder(Server(body=b"{}"))

        await embedder.aclose()

        assert embedder._client.is_closed

    async def test_vectors_come_back_in_the_order_they_were_asked_for(self) -> None:
        """Ordered by the index the server reports, not by arrival.

        A response that came back out of order would attach every embedding
        to the wrong text, and nothing downstream could detect it.
        """
        body = json.dumps(
            {
                "data": [
                    {"index": 1, "embedding": [0.5, 0.5, 0.5, 0.5]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                ]
            }
        ).encode()
        server = Server(body=body)

        vectors = await self.embedder(server).embed(["first", "second"])

        assert vectors[0] == [1.0, 0.0, 0.0, 0.0]
        assert vectors[1] == [0.5, 0.5, 0.5, 0.5]
        assert server.last_json == {"model": "nomic-embed-text", "input": ["first", "second"]}

    async def test_an_empty_batch_never_leaves_the_process(self) -> None:
        server = Server(body=b"{}")
        assert await self.embedder(server).embed([]) == []
        assert server.requests == []

    async def test_a_width_that_disagrees_with_the_column_is_refused(self) -> None:
        """Vectors of different widths cannot be compared at all, so a
        mismatch has to be loud rather than silently unsearchable."""
        body = json.dumps({"data": [{"index": 0, "embedding": [1.0, 2.0]}]}).encode()

        with pytest.raises(AIConfigurationError) as caught:
            await self.embedder(Server(body=body), dimensions=1536).embed(["x"])

        assert caught.value.code == "ai_embedding_dimension_mismatch"

    async def test_a_short_response_is_refused(self) -> None:
        body = json.dumps({"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]}).encode()

        with pytest.raises(AIProviderError) as caught:
            await self.embedder(Server(body=body)).embed(["one", "two"])

        assert caught.value.code == "ai_embedding_count_mismatch"

    async def test_an_unreachable_embedder_is_unavailable(self) -> None:
        with pytest.raises(AIUnavailableError):
            await self.embedder(Server(raises=httpx.ConnectError("refused"))).embed(["x"])

    async def test_identity(self) -> None:
        embedder = self.embedder(Server(), dimensions=1536)
        # The model is part of the name: two models behind one endpoint are
        # two incomparable spaces, and the name on each memory row is what
        # keeps them apart.
        assert embedder.name == f"openai_compatible:{embedder._model}"
        assert embedder.dimensions == 1536

    async def test_a_key_is_sent_when_configured(self) -> None:
        body = json.dumps({"data": [{"index": 0, "embedding": [0.0, 0.0, 0.0, 0.0]}]}).encode()
        server = Server(body=body)

        await self.embedder(server, api_key="sk-local").embed(["x"])

        assert server.requests[-1].headers["authorization"] == "Bearer sk-local"


class TestIdentity:
    def test_name_model_and_tool_support(self) -> None:
        chat = provider(Server(), model="llama-3.3")

        assert chat.name == "openai_compatible"
        assert chat.model == "llama-3.3"
        assert chat.supports_tools is True

    async def test_closing_releases_the_client(self) -> None:
        chat = provider(Server())
        await chat.aclose()


class TestFragmentsThatDoNotAddUp:
    async def test_a_call_that_never_received_a_name_is_dropped(self) -> None:
        """Fragments arrive out of any order the server likes.

        One that only ever carried arguments cannot be run -- there is no
        tool to run -- and inventing a name would mean running the wrong one.
        """
        server = Server(
            body=sse(
                [
                    call_fragment(index=0, call_id="c1", arguments='{"path":'),
                    call_fragment(index=0, arguments=' "/repo"}'),
                    text_chunk("", finish="tool_calls"),
                ]
            )
        )

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        assert calls_of(collected) == []

    async def test_a_fragment_with_no_index_is_treated_as_the_first_call(self) -> None:
        """Some servers omit it when there is only one call in flight.

        Dropping the fragment would lose the call entirely; assuming index
        zero reassembles it, and a server that omits the index is not
        sending a second call in parallel.
        """
        server = Server(
            body=sse(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "id": "c1",
                                            "function": {
                                                "name": "git_status",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                    text_chunk("", finish="tool_calls"),
                ]
            )
        )

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        assert [call.name for call in calls_of(collected)] == ["git_status"]

    async def test_a_tool_call_fragment_that_is_not_an_object_is_skipped(self) -> None:
        server = Server(
            body=sse(
                [
                    {"choices": [{"index": 0, "delta": {"tool_calls": ["not an object"]}}]},
                    text_chunk("still here", finish="stop"),
                ]
            )
        )

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        assert text_of(collected) == ["still here"]
        assert calls_of(collected) == []

    async def test_a_chunk_whose_delta_is_not_an_object_is_skipped(self) -> None:
        """A keep-alive frame, or a server reporting a choice with no delta
        at all. Indexing it would raise inside the stream."""
        server = Server(
            body=sse(
                [
                    {"choices": [{"index": 0, "delta": None, "finish_reason": None}]},
                    text_chunk("hi", finish="stop"),
                ]
            )
        )

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        assert text_of(collected) == ["hi"]

    async def test_a_stream_that_ends_without_the_done_sentinel_still_completes(self) -> None:
        """A server that closes the connection rather than sending [DONE].

        The assembled tool calls have to be emitted anyway, or a reply that
        asked to run something would silently do nothing.
        """
        server = Server(
            body=sse(
                [
                    call_fragment(index=0, call_id="c1", name="git_status", arguments="{}"),
                    text_chunk("", finish="tool_calls"),
                ],
                terminate=False,
            )
        )

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        assert [call.name for call in calls_of(collected)] == ["git_status"]
        assert isinstance(collected[-1], StreamCompleted)

    async def test_a_non_streamed_call_with_no_name_is_dropped(self) -> None:
        """The same rule on the ``complete`` path, where the whole message
        arrives at once."""
        server = Server(
            body=json.dumps(
                {
                    "model": "local-model",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "c1", "type": "function", "function": {"name": ""}}
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            ).encode()
        )

        completion = await provider(server).complete(REQUEST)

        assert completion.tool_calls == ()
