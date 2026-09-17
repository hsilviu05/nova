"""Adapter for any server speaking the OpenAI chat-completions wire format.

llama.cpp's server, vLLM, LM Studio, Ollama's ``/v1`` endpoint, and most
self-hosted gateways all speak it. One adapter covers them because the format
is the contract -- which is the point of ADR 002 and the reason this is a
separate file from the Ollama one rather than a flag inside it.

The format's streaming shape is genuinely different from Ollama's: tool-call
arguments arrive as a *token stream* spread across many chunks, accumulated
by index. That accumulation is the bulk of this module, and getting it wrong
means acting on half-written arguments -- so a call is only emitted once the
stream says the model has stopped choosing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx

from nova.ai.base import (
    ChatCompletion,
    ChatMessage,
    ChatRequest,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolCallRequested,
)
from nova.ai.errors import (
    AIConfigurationError,
    AIProviderError,
    AIUnavailableError,
)
from nova.core.logging import get_logger
from nova.tools.safety import wrap_tool_output

logger = get_logger(__name__)


@dataclass
class _PartialCall:
    """A tool call being assembled from streamed fragments."""

    id: str = ""
    name: str = ""
    arguments: str = ""

    def finish(self) -> ToolCall | None:
        """Build the call, or ``None`` if what arrived is not usable.

        Unparseable arguments are dropped rather than passed along as an
        empty object: a tool invoked with silently-missing arguments does
        something other than what the model asked for.
        """
        if not self.name:
            return None

        raw = self.arguments.strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("openai_tool_arguments_unparseable", tool=self.name)
            return None

        if not isinstance(parsed, dict):
            return None
        return ToolCall(id=self.id or uuid.uuid4().hex, name=self.name, arguments=parsed)


@dataclass
class _StreamState:
    """Everything accumulated across one streamed response."""

    calls: dict[int, _PartialCall] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str | None = None


class OpenAICompatibleChatProvider:
    """Chat completions over the OpenAI wire format."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, read=None),
            transport=transport,
        )

    @property
    def name(self) -> str:
        return "openai_compatible"

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tools(self) -> bool:
        return True

    # -- requests ---------------------------------------------------------

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent, None]:
        payload = self._build_payload(request, stream=True)
        state = _StreamState()

        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                await _raise_for_status(response)

                async for line in response.aiter_lines():
                    chunk = _sse_data(line)
                    if chunk is None:
                        continue
                    if chunk == "[DONE]":
                        break

                    try:
                        frame = json.loads(chunk)
                    except json.JSONDecodeError:
                        logger.warning("openai_unparseable_chunk", length=len(chunk))
                        continue

                    text = self._absorb(frame, state)
                    if text:
                        yield TextDelta(text)
        except httpx.HTTPError as exc:
            raise _translate(exc) from exc

        # Emitted only now: a call assembled from fragments is not a request
        # until the fragments stop arriving.
        for index in sorted(state.calls):
            call = state.calls[index].finish()
            if call is not None:
                yield ToolCallRequested(call)

        yield StreamCompleted(
            stop_reason=_stop_reason(state.finish_reason, bool(state.calls)),
            usage=state.usage,
            model=state.model or self._model,
        )

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        payload = self._build_payload(request, stream=False)

        try:
            response = await self._client.post("/chat/completions", json=payload)
            await _raise_for_status(response)
            frame = response.json()
        except httpx.HTTPError as exc:
            raise _translate(exc) from exc
        except json.JSONDecodeError as exc:
            raise AIProviderError("The model server returned a body that is not JSON.") from exc

        choices = frame.get("choices") or [{}]
        message = choices[0].get("message") or {}
        calls = _whole_tool_calls(message)

        return ChatCompletion(
            text=str(message.get("content") or ""),
            model=str(frame.get("model") or self._model),
            usage=_usage_from(frame.get("usage")),
            tool_calls=tuple(calls),
            stop_reason=_stop_reason(choices[0].get("finish_reason"), bool(calls)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals --------------------------------------------------------

    def _absorb(self, frame: dict[str, Any], state: _StreamState) -> str:
        """Fold one chunk into ``state``, returning any visible text in it."""
        if model := frame.get("model"):
            state.model = str(model)
        if usage := frame.get("usage"):
            # Sent once at the end by servers that report it at all.
            state.usage = _usage_from(usage)

        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""

        choice = choices[0]
        if reason := choice.get("finish_reason"):
            state.finish_reason = str(reason)

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return ""

        for fragment in delta.get("tool_calls") or []:
            if isinstance(fragment, dict):
                _absorb_call_fragment(fragment, state)

        content = delta.get("content")
        return content if isinstance(content, str) else ""

    def _build_payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        if request.context:
            messages.append({"role": "system", "content": request.context})

        for message in request.messages:
            messages.extend(_as_openai_messages(message))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        return payload


class OpenAICompatibleEmbeddingProvider:
    """Embeddings over the OpenAI wire format.

    The width is declared by configuration and checked against the memories
    column at startup rather than discovered here, because a mismatch has to
    fail before anything is written -- vectors of different widths cannot be
    compared, and a half-embedded store is worse than one that never started.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def name(self) -> str:
        # The model is part of the identity: two models behind the same
        # server produce incomparable vectors, and the provider name on each
        # memory row is what stops them being compared.
        return f"openai_compatible:{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            response = await self._client.post(
                "/embeddings", json={"model": self._model, "input": texts}
            )
            await _raise_for_status(response)
            frame = response.json()
        except httpx.HTTPError as exc:
            raise _translate(exc) from exc

        data = frame.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise AIProviderError(
                "The embedding server returned a different number of vectors than texts.",
                code="ai_embedding_count_mismatch",
            )

        vectors: list[list[float]] = []
        for element in sorted(data, key=lambda item: item.get("index", 0)):
            vector = element.get("embedding")
            if not isinstance(vector, list) or len(vector) != self._dimensions:
                raise AIConfigurationError(
                    f"Embedding model {self._model} returned "
                    f"{len(vector) if isinstance(vector, list) else 'no'} dimensions, "
                    f"but NOVA is configured for {self._dimensions}.",
                    code="ai_embedding_dimension_mismatch",
                )
            vectors.append([float(value) for value in vector])
        return vectors

    async def aclose(self) -> None:
        await self._client.aclose()


# -- shared helpers -----------------------------------------------------------


def _sse_data(line: str) -> str | None:
    """The payload of one ``data:`` line, or ``None`` for anything else."""
    if not line.startswith("data:"):
        return None
    return line[len("data:") :].strip()


def _absorb_call_fragment(fragment: dict[str, Any], state: _StreamState) -> None:
    """Merge one streamed tool-call fragment into the partial call it belongs to.

    Keyed by ``index`` rather than by id, because the id is only present on
    the first fragment of a call while the index is on all of them.
    """
    index = fragment.get("index")
    if not isinstance(index, int):
        index = 0

    partial = state.calls.setdefault(index, _PartialCall())
    if call_id := fragment.get("id"):
        partial.id = str(call_id)

    function = fragment.get("function")
    if not isinstance(function, dict):
        return
    if name := function.get("name"):
        partial.name = str(name)
    if (arguments := function.get("arguments")) is not None:
        partial.arguments += str(arguments)


def _whole_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Read tool calls from a non-streamed message."""
    calls: list[ToolCall] = []
    for element in message.get("tool_calls") or []:
        if not isinstance(element, dict):
            continue
        function = element.get("function")
        if not isinstance(function, dict):
            continue

        partial = _PartialCall(
            id=str(element.get("id") or ""),
            name=str(function.get("name") or ""),
            arguments=str(function.get("arguments") or ""),
        )
        if (call := partial.finish()) is not None:
            calls.append(call)
    return calls


def _as_openai_messages(message: ChatMessage) -> list[dict[str, Any]]:
    """Render one NOVA turn as the one or more turns the format expects."""
    if message.tool_results:
        return [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": wrap_tool_output(result.name, result.content, is_error=result.is_error),
            }
            for result in message.tool_results
        ]

    rendered: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        rendered["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    return [rendered]


def _usage_from(usage: Any) -> TokenUsage:
    if not isinstance(usage, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


def _stop_reason(finish_reason: str | None, had_calls: bool) -> str | None:
    """Translate into the vocabulary the rest of NOVA already reads."""
    if had_calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason is None:
        return None
    return {"stop": "end_turn", "length": "max_tokens"}.get(finish_reason, finish_reason)


async def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return

    body = await response.aread()
    detail = body.decode("utf-8", "replace")[:300]
    logger.warning("openai_compatible_error", status=response.status_code, detail=detail)

    if response.status_code in (401, 403):
        raise AIConfigurationError(
            "The model server rejected NOVA's credentials.", code="ai_credentials_rejected"
        )
    raise AIUnavailableError(
        f"The model server answered {response.status_code}. {detail}".strip(),
        code="ai_local_model_error",
    )


def _translate(exc: httpx.HTTPError) -> AIProviderError:
    if isinstance(exc, httpx.ConnectError):
        return AIUnavailableError(
            "The model server is not reachable.", code="ai_local_model_unreachable"
        )
    if isinstance(exc, httpx.TimeoutException):
        return AIUnavailableError(
            "The model took too long to answer.", code="ai_local_model_timeout"
        )
    logger.warning("openai_compatible_request_failed", error=type(exc).__name__)
    return AIProviderError()
