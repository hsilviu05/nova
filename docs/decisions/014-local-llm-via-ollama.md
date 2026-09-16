# 014 — A local model through Ollama, natively on the host

**Status:** Accepted · **Date:** 2026-09-07

## Context

NOVA's chat runs through a provider abstraction (ADR 002) with two adapters:
Anthropic, and an openly canned offline provider that keeps the device
answering when nothing else can. The owner's machine has enough memory to
run a large open model, and a companion that listens on a desk has an
obvious reason to prefer one: nothing it hears has to leave the house.

## Decision

**Add Ollama as a third chat provider, `NOVA_AI__CHAT_PROVIDER=ollama`.**
It is an adapter behind the same protocol; the conversation service, memory
extraction and every test above the adapter are unchanged.

**Ollama runs natively on the host, never inside the compose stack.** Docker
on macOS cannot reach Metal, so a containerised Ollama would run on CPU and
be an order of magnitude slower. The API container reaches the host at
`host.docker.internal:11434`; `extra_hosts` makes that name resolve on Linux
as well.

**No boot-time fallback to offline.** The Anthropic adapter degrades to
offline when its key is missing, because a missing credential is known at
boot. Whether a local server is up is a runtime fact. If it is not, calls
raise `AIUnavailableError` and the API answers 503, which is the truth and
is what the phone already handles.

**`keep_alive` is sent with every request, default 30 minutes.** Ollama
unloads an idle model after five minutes. A desk companion is talked to
sporadically, so the default would put a cold load of tens of gigabytes in
front of most replies.

**One system message, persona first, context appended.** Ollama reuses its
KV cache when a prompt shares a prefix with the previous one. Keeping the
persona byte-identical at the front and appending the volatile memory
context buys the same thing Anthropic's `cache_control` does, without
depending on how a given chat template renders two system messages.

**Default model `qwen2.5:32b`.** Apache-2.0, about 20 GB at the default
quantisation, and good at the structured JSON that memory extraction asks
for. `qwen2.5:7b` for a laptop; `llama3.3:70b` (Llama licence, ~43 GB) as
the quality tier. Configurable, because the right answer depends on the
machine.

## What this does not change

**Embeddings.** Ollama serves embedding models too, and ADR 011 is candid
that the lexical hashing embeddings are not semantic. But `Memory.embedding`
is a `Vector(1536)` column and the common local embedding models are 768 or
1024 wide, so that is a migration and a re-embed of every stored memory.
A separate decision, made when the first real memories exist to re-embed.

**CI.** It cannot run a model. The tests exercise the adapter against a fake
server — the exact request it sends, and how it treats every kind of
response — and the offline provider remains what the suite talks to.

## Consequences

- Memory extraction quality depends on the model. Small local models emit
  malformed JSON more often; the extractor already degrades to silence on
  any failure, so the failure mode is fewer memories, never a crash.
- First-token latency on a cold load is seconds, not milliseconds, even
  for a 7B model. The adapter's connect timeout is short and its read
  timeout is the configured request timeout; `keep_alive` is what makes the
  second reply fast.
- Found while wiring this: `docker-compose.yml` never forwarded any
  `NOVA_AI__*` variable into the container, so `CHAT_PROVIDER=anthropic` in
  `.env` was silently ignored under compose. Fixed in the same change.
