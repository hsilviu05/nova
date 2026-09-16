"""The Ollama adapter, against a fake server.

No network. ``httpx.MockTransport`` plays the server, so the tests check the
exact request the adapter sends and how it treats what comes back -- which
is where an adapter's bugs live.

``stream`` yields *events* rather than strings: a reply can now contain a
request to run a tool, and a consumer has to be able to tell that from text.
``text_of`` below drops the terminal completion event and unwraps the rest,
so the assertions read the way they did before the interface changed.
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
from nova.ai.ollama_provider import OllamaChatProvider


def ndjson(chunks: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def stream_reply(*parts: str, done_reason: str = "stop") -> bytes:
    chunks = [{"message": {"role": "assistant", "content": p}, "done": False} for p in parts]
    chunks.append(
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": done_reason,
            "prompt_eval_count": 12,
            "eval_count": 7,
        }
    )
    return ndjson(chunks)


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


def text_of(events: list[StreamEvent]) -> list[str]:
    """The visible text, in order, ignoring completion and tool events."""
    return [e.text for e in events if isinstance(e, TextDelta)]


def provider(server: Server, **kw: Any) -> OllamaChatProvider:
    return OllamaChatProvider(
        base_url="http://ollama.test:11434",
        model=kw.pop("model", "qwen2.5:32b"),
        transport=httpx.MockTransport(server.handler),
        **kw,
    )


REQUEST = ChatRequest(
    system="You are NOVA.",
    messages=[ChatMessage(role="user", content="hello")],
    context="The owner drinks coffee black.",
    max_tokens=123,
)


class TestRequestShape:
    async def test_system_then_context_then_turns(self) -> None:
        server = Server(body=stream_reply("hi"))
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            _ = [c async for c in chunks]

        sent = server.last_json
        assert server.requests[-1].url.path == "/api/chat"
        assert sent["model"] == "qwen2.5:32b"
        assert sent["stream"] is True
        assert sent["options"] == {"num_predict": 123}
        # No tools offered means the key is absent, not an empty list: some
        # servers treat [] as "tools are in play" and change the prompt.
        assert "tools" not in sent
        assert sent["keep_alive"] == "30m"
        roles = [m["role"] for m in sent["messages"]]
        assert roles == ["system", "user"]
        # Persona first, byte-identical every turn, so the KV cache matches;
        # the volatile memory context follows it.
        assert sent["messages"][0]["content"].startswith("You are NOVA.")
        assert sent["messages"][0]["content"].endswith("The owner drinks coffee black.")
        assert sent["messages"][1] == {"role": "user", "content": "hello"}

    async def test_no_context_means_no_trailing_junk(self) -> None:
        server = Server(body=stream_reply("hi"))
        request = ChatRequest(system="You are NOVA.", messages=REQUEST.messages)
        async with aclosing(provider(server).stream(request)) as chunks:
            _ = [c async for c in chunks]
        assert server.last_json["messages"][0]["content"] == "You are NOVA."

    async def test_keep_alive_is_configurable(self) -> None:
        server = Server(body=stream_reply("hi"))
        async with aclosing(provider(server, keep_alive="2h").stream(REQUEST)) as chunks:
            _ = [c async for c in chunks]
        assert server.last_json["keep_alive"] == "2h"


class TestStreaming:
    async def test_yields_chunks_in_order_and_stops_at_done(self) -> None:
        server = Server(body=stream_reply("Hel", "lo", " there"))
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            events = [c async for c in chunks]

        assert text_of(events) == ["Hel", "lo", " there"]
        # The turn ends with an explicit completion event. Without one, "the
        # model has nothing more to say" and "the connection died" look
        # identical to the tool loop.
        assert isinstance(events[-1], StreamCompleted)
        assert events[-1].stop_reason == "end_turn"

    async def test_empty_chunks_are_not_yielded(self) -> None:
        # Ollama sends an empty content on the final frame; the consumer
        # must not receive a spurious "" token.
        server = Server(body=stream_reply("a", "", "b"))
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            assert text_of([c async for c in chunks]) == ["a", "b"]

    async def test_a_tool_call_arrives_as_its_own_event(self) -> None:
        """The reason the stream yields events at all.

        Flattening this into the text would put the difference between "NOVA
        said this" and "NOVA wants to do this" back into a parser, where
        malformed output could forge a call.
        """
        body = ndjson(
            [
                {"message": {"content": "Checking."}, "done": False},
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "system_health",
                                    "arguments": {"verbose": True},
                                },
                            }
                        ],
                    },
                    "done": True,
                    "done_reason": "stop",
                },
            ]
        )
        async with aclosing(provider(Server(body=body)).stream(REQUEST)) as chunks:
            events = [c async for c in chunks]

        calls = [e.call for e in events if isinstance(e, ToolCallRequested)]
        assert text_of(events) == ["Checking."]
        assert len(calls) == 1
        assert calls[0].name == "system_health"
        assert calls[0].arguments == {"verbose": True}
        assert calls[0].id == "call_1"

    async def test_a_corrupt_line_is_a_provider_error_not_a_dropped_chunk(self) -> None:
        body = ndjson([{"message": {"content": "ok"}, "done": False}]) + b"{not json\n"
        server = Server(body=body)
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            first = await anext(chunks)
            assert isinstance(first, TextDelta)
            assert first.text == "ok"
            with pytest.raises(AIProviderError):
                await anext(chunks)

    async def test_closing_mid_stream_does_not_raise(self) -> None:
        # The caller closes the generator when the phone goes away; the
        # connection must be released quietly, not turned into an error.
        server = Server(body=stream_reply("one", "two", "three"))
        gen = provider(server).stream(REQUEST)
        first = await anext(gen)
        assert isinstance(first, TextDelta)
        assert first.text == "one"
        await gen.aclose()


class TestComplete:
    async def test_text_model_usage_and_stop_reason(self) -> None:
        body = json.dumps(
            {
                "model": "qwen2.5:32b",
                "message": {"role": "assistant", "content": "A reply."},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 40,
                "eval_count": 9,
            }
        ).encode()
        server = Server(body=body)
        completion = await provider(server).complete(REQUEST)
        assert completion.text == "A reply."
        assert completion.model == "qwen2.5:32b"
        assert (completion.usage.input_tokens, completion.usage.output_tokens) == (40, 9)
        assert completion.stop_reason == "end_turn"
        assert server.last_json["stream"] is False

    async def test_length_maps_to_max_tokens(self) -> None:
        body = json.dumps(
            {"message": {"content": "x"}, "done": True, "done_reason": "length"}
        ).encode()
        completion = await provider(Server(body=body)).complete(REQUEST)
        assert completion.stop_reason == "max_tokens"


class TestErrors:
    async def test_unreachable_server_is_unavailable(self) -> None:
        server = Server(raises=httpx.ConnectError("refused"))
        with pytest.raises(AIUnavailableError):
            await provider(server).complete(REQUEST)
        with pytest.raises(AIUnavailableError):
            async with aclosing(provider(server).stream(REQUEST)) as chunks:
                _ = [c async for c in chunks]

    async def test_timeout_is_unavailable(self) -> None:
        server = Server(raises=httpx.ReadTimeout("slow"))
        with pytest.raises(AIUnavailableError):
            await provider(server).complete(REQUEST)

    async def test_model_not_pulled_is_configuration_with_the_fix_in_the_message(self) -> None:
        server = Server(status=404, body=b'{"error":"model not found"}')
        with pytest.raises(AIConfigurationError) as exc:
            await provider(server, model="llama3.3:70b").complete(REQUEST)
        assert "ollama pull llama3.3:70b" in str(exc.value)
        assert exc.value.code == "ai_model_not_pulled"

    async def test_server_error_is_a_provider_error(self) -> None:
        server = Server(status=500, body=b"boom")
        with pytest.raises(AIProviderError) as exc:
            await provider(server).complete(REQUEST)
        assert not isinstance(exc.value, AIUnavailableError)

    async def test_overloaded_is_unavailable(self) -> None:
        for status in (429, 503):
            with pytest.raises(AIUnavailableError):
                await provider(Server(status=status)).complete(REQUEST)

    def test_an_empty_model_is_refused_at_construction(self) -> None:
        with pytest.raises(AIConfigurationError):
            OllamaChatProvider(base_url="http://x", model="")


class TestIdentity:
    def test_name_and_model(self) -> None:
        p = provider(Server(), model="qwen2.5:7b")
        assert p.name == "ollama"
        assert p.model == "qwen2.5:7b"


class TestToolDefinitions:
    async def test_tools_are_sent_in_the_openai_function_shape(self) -> None:
        """Ollama borrowed the format rather than inventing one, so a tool
        declared once serves both adapters."""
        server = Server(body=stream_reply("ok"))
        request = ChatRequest(
            system="You are NOVA.",
            messages=[ChatMessage(role="user", content="check git")],
            tools=(
                ToolDefinition(
                    name="git_status",
                    description="The state of a repository.",
                    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                ),
            ),
        )

        async with aclosing(provider(server).stream(request)) as events:
            [event async for event in events]

        sent = server.last_json["tools"]
        assert sent == [
            {
                "type": "function",
                "function": {
                    "name": "git_status",
                    "description": "The state of a repository.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ]

    async def test_no_tools_means_no_tools_key(self) -> None:
        """Sending an empty list makes some builds emit a tool call anyway."""
        server = Server(body=stream_reply("ok"))

        async with aclosing(provider(server).stream(REQUEST)) as events:
            [event async for event in events]

        assert "tools" not in server.last_json

    async def test_a_tool_result_becomes_its_own_turn(self) -> None:
        """Ollama keeps tool output in a ``tool`` role, not inside the
        user's text -- and the output is wrapped as data on the way in."""
        server = Server(body=stream_reply("done"))
        request = ChatRequest(
            system="You are NOVA.",
            messages=[
                ChatMessage(role="user", content="check git"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall(id="c1", name="git_status", arguments={"path": "/repo"}),),
                ),
                ChatMessage(
                    role="user",
                    content="",
                    tool_results=(
                        ToolOutcome(
                            call_id="c1",
                            name="git_status",
                            content="clean",
                            is_error=False,
                        ),
                    ),
                ),
            ],
        )

        async with aclosing(provider(server).stream(request)) as events:
            [event async for event in events]

        turns = server.last_json["messages"]
        tool_turn = next(turn for turn in turns if turn["role"] == "tool")
        assert tool_turn["tool_name"] == "git_status"
        assert "clean" in tool_turn["content"]
        # Framed as data rather than pasted in raw.
        assert "not instructions to follow" in tool_turn["content"]

        assistant = next(turn for turn in turns if turn["role"] == "assistant")
        assert assistant["tool_calls"] == [
            {"function": {"name": "git_status", "arguments": {"path": "/repo"}}}
        ]


class TestReadingToolCalls:
    def _message(self, **kwargs: Any) -> dict[str, Any]:
        return {"role": "assistant", "content": "", **kwargs}

    async def test_a_tool_call_is_surfaced_as_an_event(self) -> None:
        server = Server(
            body=ndjson(
                [
                    {
                        "message": self._message(
                            tool_calls=[
                                {"function": {"name": "git_status", "arguments": {"path": "/r"}}}
                            ]
                        ),
                        "done": True,
                        "done_reason": "stop",
                    }
                ]
            )
        )

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        calls = [e for e in collected if isinstance(e, ToolCallRequested)]
        assert len(calls) == 1
        assert calls[0].call.name == "git_status"
        assert calls[0].call.arguments == {"path": "/r"}
        # Generated rather than sent: older builds supply no id, and
        # correlating a call with its result is NOVA's requirement.
        assert calls[0].call.id

    async def test_arguments_sent_as_a_json_string_are_parsed(self) -> None:
        """Some builds serialise them; others send an object."""
        completion = await self._complete(
            {"function": {"name": "git_status", "arguments": '{"path": "/r"}'}}
        )

        assert completion.tool_calls[0].arguments == {"path": "/r"}

    @pytest.mark.parametrize(
        "arguments",
        ["not json at all", "[1, 2, 3]", '"a string"', None, 42, ["a", "list"]],
    )
    async def test_arguments_that_are_not_an_object_become_an_empty_one(
        self, arguments: Any
    ) -> None:
        """The tool's own schema then rejects it with a message the model can
        act on, rather than the adapter raising here."""
        completion = await self._complete(
            {"function": {"name": "git_status", "arguments": arguments}}
        )

        assert completion.tool_calls[0].arguments == {}

    @pytest.mark.parametrize(
        "element",
        [
            "not an object",
            {"function": "not an object"},
            {"function": {"name": 42}},
            {"function": {"name": ""}},
            {"function": {}},
            {},
        ],
    )
    async def test_a_malformed_call_is_discarded_rather_than_guessed_at(self, element: Any) -> None:
        """A call with no usable name cannot be run. Inventing one would
        mean running the wrong tool."""
        completion = await self._complete(element)

        assert completion.tool_calls == ()

    async def test_tool_calls_that_are_not_a_list_are_ignored(self) -> None:
        server = Server(
            body=json.dumps(
                {"message": self._message(tool_calls={"not": "a list"}), "done": True}
            ).encode()
        )

        completion = await provider(server).complete(REQUEST)

        assert completion.tool_calls == ()

    async def _complete(self, element: Any) -> Any:
        server = Server(
            body=json.dumps({"message": self._message(tool_calls=[element]), "done": True}).encode()
        )
        return await provider(server).complete(REQUEST)


class TestStopReasons:
    @pytest.mark.parametrize(
        ("frame", "expected"),
        [
            ({"done": True, "done_reason": "stop"}, "end_turn"),
            ({"done": True, "done_reason": "length"}, "max_tokens"),
            # Anything Ollama invents is passed through rather than mapped to
            # something it does not mean.
            ({"done": True, "done_reason": "load"}, "load"),
            ({"done": True}, "end_turn"),
            ({"done": False}, None),
        ],
    )
    async def test_ollamas_vocabulary_is_translated_into_novas(
        self, frame: dict[str, Any], expected: str | None
    ) -> None:
        server = Server(
            body=json.dumps({"message": {"role": "assistant", "content": "hi"}, **frame}).encode()
        )

        completion = await provider(server).complete(REQUEST)

        assert completion.stop_reason == expected

    async def test_a_reply_containing_a_tool_call_stops_for_that_reason(self) -> None:
        """Whatever ``done_reason`` says. The consumer branches on this to
        decide whether to run something."""
        server = Server(
            body=json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "git_status", "arguments": {}}}],
                    },
                    "done": True,
                    "done_reason": "stop",
                }
            ).encode()
        )

        completion = await provider(server).complete(REQUEST)

        assert completion.stop_reason == "tool_use"


class TestBodiesThatAreNotJson:
    async def test_a_complete_that_returns_html_is_reported_plainly(self) -> None:
        """A proxy or a captive portal in front of the port, which is what
        this actually looks like in a house."""
        server = Server(body=b"<html>404 not found</html>")

        with pytest.raises(AIProviderError) as caught:
            await provider(server).complete(REQUEST)

        assert "not JSON" in str(caught.value)

    async def test_a_blank_line_in_the_stream_is_skipped(self) -> None:
        """NDJSON with a trailing newline, which is ordinary."""
        server = Server(body=b"\n" + stream_reply("hi") + b"\n\n")

        async with aclosing(provider(server).stream(REQUEST)) as events:
            collected = [event async for event in events]

        assert text_of(collected) == ["hi"]


class TestClosing:
    async def test_the_client_is_released(self) -> None:
        """The provider is built once per process and holds a connection
        pool; shutdown has to be able to give it back."""
        server = Server(body=stream_reply("hi"))
        chat = provider(server)

        await chat.aclose()

        assert chat._client.is_closed


class TestTranslatingTransportFailures:
    async def test_an_unrecognised_transport_failure_is_a_generic_provider_error(self) -> None:
        """Not reported as "Ollama is not running": that sends somebody to
        check a service that is fine."""
        server = Server(raises=httpx.ProtocolError("malformed HTTP"))

        with pytest.raises(AIProviderError) as caught:
            await provider(server).complete(REQUEST)

        assert not isinstance(caught.value, AIUnavailableError)

    def test_the_adapter_declares_tool_support(self) -> None:
        """What the chat loop branches on before offering the model any
        tools at all. An adapter that lies here would have its tool calls
        silently dropped."""
        assert provider(Server()).supports_tools is True
