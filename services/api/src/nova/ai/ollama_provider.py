"""Ollama adapter for :class:`~nova.ai.base.ChatProvider`.

The intended provider for a NOVA running on someone's own machine: the model
never leaves the network, and nothing anyone says to the terminal reaches a
vendor.

Spoken to over plain HTTP rather than through a client library. Ollama's
``/api/chat`` is one POST returning newline-delimited JSON, and a dependency
whose whole job is to build that request would be a dependency for no reason.

Tool calls arrive complete rather than as a token stream -- Ollama emits the
whole ``tool_calls`` array in one object once the model has finished choosing
-- so there is no partial-argument accumulation here, unlike the
OpenAI-format adapter.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
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
from nova.ai.errors import AIConfigurationError, AIProviderError, AIUnavailableError
from nova.core.logging import get_logger
from nova.tools.safety import wrap_tool_output

logger = get_logger(__name__)


class OllamaChatProvider:
    """Chat completions through a local Ollama server."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        keep_alive: str = "30m",
        context_tokens: int = 16384,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model:
            # Caught here rather than on the first request: an empty model is
            # a configuration mistake, and Ollama's own error for it is
            # opaque.
            raise AIConfigurationError("No Ollama model configured.", code="ai_missing_model")

        self._model = model
        self._keep_alive = keep_alive
        self._context_tokens = context_tokens
        self._base_url = base_url.rstrip("/")
        # No overall read timeout: a local model legitimately takes as long
        # as it takes to think, and there is no bill running. Connect and
        # write timeouts still bound the case where nothing is listening.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, read=None),
            transport=transport,
        )

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_tools(self) -> bool:
        return True

    # -- requests ---------------------------------------------------------

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamEvent, None]:
        """Yield reply events as the local model produces them.

        Raises:
            AIUnavailableError: if Ollama is not running or the model is not
                pulled. Both are the same thing from the caller's side --
                there is no model to answer with -- and both are things the
                owner of the machine can fix.
        """
        payload = self._build_payload(request, stream=True)

        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                await self._raise_for_status(response)

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue

                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError as exc:
                        # Not skipped. A dropped frame means NOVA says
                        # something other than what the model said, and a
                        # reply silently missing a sentence is worse than a
                        # reply that failed.
                        logger.warning("ollama_unparseable_frame", length=len(line))
                        raise AIProviderError(
                            "The local model server sent a malformed frame.",
                            code="ai_malformed_stream",
                        ) from exc

                    for event in self._events_from(frame):
                        yield event
        except httpx.HTTPError as exc:
            raise self._translate(exc) from exc

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        """Return a whole reply at once."""
        payload = self._build_payload(request, stream=False)

        try:
            response = await self._client.post("/api/chat", json=payload)
            await self._raise_for_status(response)
            frame = response.json()
        except httpx.HTTPError as exc:
            raise self._translate(exc) from exc
        except json.JSONDecodeError as exc:
            raise AIProviderError("Ollama returned a body that is not JSON.") from exc

        message = frame.get("message") or {}
        return ChatCompletion(
            text=str(message.get("content") or ""),
            model=str(frame.get("model") or self._model),
            usage=_usage_from(frame),
            tool_calls=tuple(_tool_calls_from(message)),
            stop_reason=_stop_reason_from(frame, message),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals --------------------------------------------------------

    def _events_from(self, frame: dict[str, Any]) -> list[StreamEvent]:
        """Translate one NDJSON frame into zero or more events."""
        events: list[StreamEvent] = []
        message = frame.get("message") or {}

        text = message.get("content")
        if isinstance(text, str) and text:
            events.append(TextDelta(text))

        for call in _tool_calls_from(message):
            events.append(ToolCallRequested(call))

        if frame.get("done"):
            events.append(
                StreamCompleted(
                    stop_reason=_stop_reason_from(frame, message),
                    usage=_usage_from(frame),
                    model=str(frame.get("model") or self._model),
                )
            )
        return events

    def _build_payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        # One system message, persona first and byte-identical every turn,
        # with the volatile context appended. A second system turn would
        # preserve the same cacheable prefix, but some models handle only
        # one and quietly ignore or merge the rest.
        system = request.system
        if request.context:
            system = f"{system}\n\n{request.context}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

        for message in request.messages:
            messages.extend(_as_ollama_messages(message))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": stream,
            "options": {
                "num_predict": request.max_tokens,
                # Sent explicitly. Left out, Ollama allocates the model's
                # full advertised window -- 131072 tokens for llama3.2 -- and
                # a 2 GB model becomes 17.7 GB resident for a context NOVA
                # never fills.
                "num_ctx": self._context_tokens,
            },
            # Ollama unloads an idle model after five minutes by default. A
            # terminal is talked to sporadically, so that default would put a
            # cold load of several gigabytes in front of most replies.
            "keep_alive": self._keep_alive,
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

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return

        # Read the body before raising: on a streamed response it has not
        # been fetched yet, and Ollama puts the useful part ("model X not
        # found, try pulling it") in there.
        body = await response.aread()
        detail = body.decode("utf-8", "replace")[:300]
        logger.warning("ollama_error", status=response.status_code, detail=detail)

        if response.status_code == 404:
            # The model is not on this machine. An operator fixes that with
            # one command, so the message carries it -- unlike an outage,
            # which is nobody's fault and nothing to act on.
            raise AIConfigurationError(
                f"Ollama does not have {self._model}. Run: ollama pull {self._model}",
                code="ai_model_not_pulled",
            )
        if response.status_code in (429, 503):
            raise AIUnavailableError("The local model server is busy.", code="ai_local_model_busy")

        raise AIProviderError(
            f"The local model server answered {response.status_code}. {detail}".strip(),
            code="ai_local_model_error",
        )

    @staticmethod
    def _translate(exc: httpx.HTTPError) -> AIProviderError:
        if isinstance(exc, httpx.ConnectError):
            logger.warning("ollama_unreachable")
            return AIUnavailableError(
                "The local model server is not reachable. Is Ollama running?",
                code="ai_local_model_unreachable",
            )
        if isinstance(exc, httpx.TimeoutException):
            return AIUnavailableError(
                "The local model took too long to answer.", code="ai_local_model_timeout"
            )
        logger.warning("ollama_request_failed", error=type(exc).__name__)
        return AIProviderError()


def _as_ollama_messages(message: ChatMessage) -> list[dict[str, Any]]:
    """Render one NOVA turn as the one or more turns Ollama expects.

    A user turn carrying tool results becomes one ``tool`` message per
    result, because Ollama -- like the OpenAI format it borrows from -- keeps
    tool output in its own role rather than inside the user's text.
    """
    if message.tool_results:
        return [
            {
                "role": "tool",
                "tool_name": result.name,
                "content": wrap_tool_output(result.name, result.content, is_error=result.is_error),
            }
            for result in message.tool_results
        ]

    rendered: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        rendered["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    return [rendered]


def _tool_calls_from(message: dict[str, Any]) -> list[ToolCall]:
    """Read the tool calls out of an Ollama message, discarding malformed ones."""
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []

    calls: list[ToolCall] = []
    for element in raw:
        if not isinstance(element, dict):
            continue
        function = element.get("function")
        if not isinstance(function, dict):
            continue

        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue

        arguments = function.get("arguments")
        if isinstance(arguments, str):
            # Some builds serialise the arguments; others send an object.
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if not isinstance(arguments, dict):
            arguments = {}

        # Newer Ollama builds supply a call id; older ones do not. One is
        # generated when it is missing, because correlating a call with its
        # result is NOVA's requirement rather than the server's.
        call_id = element.get("id")
        calls.append(
            ToolCall(
                id=str(call_id) if isinstance(call_id, str) and call_id else uuid.uuid4().hex,
                name=name,
                arguments=arguments,
            )
        )
    return calls


def _usage_from(frame: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=int(frame.get("prompt_eval_count") or 0),
        output_tokens=int(frame.get("eval_count") or 0),
    )


def _stop_reason_from(frame: dict[str, Any], message: dict[str, Any]) -> str | None:
    if message.get("tool_calls"):
        return "tool_use"
    reason = frame.get("done_reason")
    if isinstance(reason, str) and reason:
        # Ollama says "stop"/"length"; NOVA's vocabulary is the Anthropic one
        # because that is what the rest of the codebase already reads.
        return {"stop": "end_turn", "length": "max_tokens"}.get(reason, reason)
    return "end_turn" if frame.get("done") else None
