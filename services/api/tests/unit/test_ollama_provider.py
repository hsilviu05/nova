"""The Ollama adapter, against a fake server.

No network. ``httpx.MockTransport`` plays the server, so the tests check the
exact request the adapter sends and how it treats what comes back -- which
is where an adapter's bugs live.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import aclosing
from typing import Any

import httpx
import pytest

from nova.ai.base import ChatMessage, ChatRequest
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
            assert [c async for c in chunks] == ["Hel", "lo", " there"]

    async def test_empty_chunks_are_not_yielded(self) -> None:
        # Ollama sends an empty content on the final frame; the consumer
        # must not receive a spurious "" token.
        server = Server(body=stream_reply("a", "", "b"))
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            assert [c async for c in chunks] == ["a", "b"]

    async def test_a_corrupt_line_is_a_provider_error_not_a_dropped_chunk(self) -> None:
        body = ndjson([{"message": {"content": "ok"}, "done": False}]) + b"{not json\n"
        server = Server(body=body)
        async with aclosing(provider(server).stream(REQUEST)) as chunks:
            assert await anext(chunks) == "ok"
            with pytest.raises(AIProviderError):
                await anext(chunks)

    async def test_closing_mid_stream_does_not_raise(self) -> None:
        # The caller closes the generator when the phone goes away; the
        # connection must be released quietly, not turned into an error.
        server = Server(body=stream_reply("one", "two", "three"))
        gen = provider(server).stream(REQUEST)
        assert await anext(gen) == "one"
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
