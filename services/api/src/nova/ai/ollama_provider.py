"""Ollama adapter for :class:`~nova.ai.base.ChatProvider`.

A local model on the machine that runs the backend. Nothing leaves the
house: the persona, the conversation, and the memories that get extracted
from it all stay on the owner's own hardware, which for a device that sits
on a desk and listens is not a small property.

The only module that speaks Ollama's HTTP API. Everything else sees the
protocol in :mod:`nova.ai.base`, so this is a configuration choice and not a
refactor -- ADR 002 doing its job a second time.

Two operational details are baked in rather than left to the operator:

* ``keep_alive`` is sent with every request. Ollama unloads a model after
  five idle minutes by default, and a desk companion is talked to
  sporadically -- so with the default, almost every reply would start with
  a cold load of tens of gigabytes from disk. Thirty minutes keeps the
  model resident across a conversation and lets it go overnight.

* The system prompt is one message with the stable persona first and the
  volatile context after. Ollama reuses its KV cache when a prompt shares
  a prefix with the previous one, so keeping the persona byte-identical at
  the front buys the same thing Anthropic's ``cache_control`` does.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from nova.ai.base import ChatCompletion, ChatMessage, ChatRequest, TokenUsage
from nova.ai.errors import AIConfigurationError, AIProviderError, AIUnavailableError

CHAT_PATH = "/api/chat"

# Ollama's done_reason values, mapped onto the provider-neutral vocabulary
# the Anthropic adapter already uses.
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens"}


class OllamaChatProvider:
    """Chat completions through a local Ollama server."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        keep_alive: str = "30m",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model:
            raise AIConfigurationError("No Ollama model configured.", code="ai_missing_model")
        self._model = model
        self._keep_alive = keep_alive
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # A cold 70B model can take longer than a network call to start
            # answering. The read timeout is the configured one; connect is
            # short because a server that is not there should say so fast.
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
        )

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    async def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        """Yield reply text as the model produces it.

        The HTTP stream is held inside the generator, so closing the
        generator -- which the caller does with ``aclosing`` when the client
        goes away -- releases the connection in the same step.

        Raises:
            AIUnavailableError: the server is not reachable.
            AIConfigurationError: the model is not pulled.
            AIProviderError: any other failure, including a corrupt stream.
        """
        payload = self._payload(request, stream=True)
        try:
            async with self._client.stream("POST", CHAT_PATH, json=payload) as response:
                self._raise_for_status(response.status_code, await self._error_text(response))
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = self._decode(line)
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        yield text
                    if chunk.get("done"):
                        return
        except httpx.ConnectError as exc:
            raise AIUnavailableError() from exc
        except httpx.TimeoutException as exc:
            raise AIUnavailableError() from exc
        except httpx.HTTPError as exc:
            raise AIProviderError() from exc

    async def complete(self, request: ChatRequest) -> ChatCompletion:
        payload = self._payload(request, stream=False)
        try:
            response = await self._client.post(CHAT_PATH, json=payload)
        except httpx.ConnectError as exc:
            raise AIUnavailableError() from exc
        except httpx.TimeoutException as exc:
            raise AIUnavailableError() from exc
        except httpx.HTTPError as exc:
            raise AIProviderError() from exc

        self._raise_for_status(response.status_code, response.text)
        body = self._decode(response.text)
        return ChatCompletion(
            text=body.get("message", {}).get("content", ""),
            model=str(body.get("model", self._model)),
            usage=TokenUsage(
                input_tokens=int(body.get("prompt_eval_count", 0) or 0),
                output_tokens=int(body.get("eval_count", 0) or 0),
            ),
            stop_reason=_STOP_REASONS.get(
                str(body.get("done_reason", "")), body.get("done_reason")
            ),
        )

    # -- internals ---------------------------------------------------------

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        system = request.system
        if request.context:
            # Stable prefix first, so the KV cache still matches; the
            # volatile part is appended, never interleaved.
            system = f"{system}\n\n{request.context}"

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(self._as_param(message) for message in request.messages)

        return {
            "model": self._model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self._keep_alive,
            "options": {"num_predict": request.max_tokens},
        }

    @staticmethod
    def _as_param(message: ChatMessage) -> dict[str, str]:
        return {"role": message.role, "content": message.content}

    @staticmethod
    def _decode(line: str) -> dict[str, Any]:
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            # A corrupt line mid-stream is a failure, not something to skip:
            # skipping it would silently drop a chunk of the reply, and the
            # streamer persists whatever arrived before this point anyway.
            raise AIProviderError() from exc
        if not isinstance(decoded, dict):
            raise AIProviderError()
        return decoded

    @staticmethod
    async def _error_text(response: httpx.Response) -> str:
        if response.status_code < 400:
            return ""
        return (await response.aread()).decode("utf-8", errors="replace")

    def _raise_for_status(self, status: int, text: str) -> None:
        if status < 400:
            return
        if status == 404:
            # Ollama answers 404 for a model that has not been pulled. That
            # is configuration, and the fix is one command, so say which.
            raise AIConfigurationError(
                f"Ollama has no model {self._model!r}; run `ollama pull {self._model}`.",
                code="ai_model_not_pulled",
            )
        if status in (502, 503, 504) or status == 429:
            raise AIUnavailableError()
        raise AIProviderError()
