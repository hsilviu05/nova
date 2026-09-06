# 010 — Streaming chat over SSE, with an offline provider

**Status:** Accepted · **Date:** 2026-09-06

## Context

Phase 4 gives NOVA a voice. Three questions came with it, and the answers are
independent enough to be worth recording together because each one shaped the
others.

1. How does reply text reach the app while it is still being written?
2. What happens to a reply when the connection, or the provider, gives out
   halfway?
3. How does any of this get tested, and how does NOVA behave, when there is no
   API key?

## Decision

### Server-Sent Events, not WebSocket

The reply stream is `POST /conversations/{id}/stream` returning
`text/event-stream` with four event types: `message` (the stored user turn),
`delta`, `done`, `error`.

NOVA already has a WebSocket for the device, so the obvious move was to reuse
it. Rejected: the two are not the same problem. Device traffic is genuinely
bidirectional and lives for the length of a session; a chat reply is
one-directional and lives for a few seconds. SSE rides ordinary HTTP, carries
the `Authorization` header the rest of the API already uses, survives proxies
that mishandle upgrades, and `URLSession.bytes(for:)` parses it without a
socket library — the whole client parser is about forty lines.

The user turn is echoed as the first event rather than being left to the
client. The app shows an optimistic bubble immediately, then swaps in the
stored message with its real id, so nothing renders twice and the transcript
matches the server before the reply even starts.

### The streamer owns its database sessions

A streaming response's body is produced *after* the endpoint returns, by which
point FastAPI has already closed the request-scoped session. So
`ChatStreamer` takes the session *factory*, not a session, and runs three
short transactions: verify ownership and store the user turn; stream with no
session held; store the reply.

Holding one session open across a stream would also pin a connection from the
pool for as long as the model takes to think, which on a small pool is how a
handful of slow conversations stall every other request.

Ownership is checked and the user turn stored *before* the response begins, so
an unknown conversation is an ordinary 404 rather than an error event inside a
stream the client has already started rendering.

### A partial reply is still a reply

If the provider fails after producing text, or the client disconnects
mid-stream, whatever NOVA said is persisted anyway. The write happens in a
`finally`, so it survives both an exception and the generator cancellation a
dropped connection causes.

The alternative — discard anything incomplete — is worse in exactly the case
that matters. The user watched words appear on screen. Reopening the thread to
find their question with no answer under it looks like the app lost their
conversation, which is a worse failure than a reply that stops early.

Once text has been yielded, a later failure ends the stream rather than
raising: the response is already 200 and streaming, so there is no status left
to change.

### An offline provider is a real implementation, not a mock

`OfflineChatProvider` needs no key and no network. It is the **default**, and
selecting `anthropic` without a key degrades to it with a warning rather than
refusing to boot.

It does two jobs. The test suite exercises the entire conversation path — 288
tests — with no API key, so CI needs no secret, costs nothing, and never fails
on a rate limit. And NOVA still answers when the provider is unconfigured or
unreachable, which matters for a device whose premise is that it keeps
behaving when the network does not.

Its replies are deterministic and openly canned ("My reasoning is offline").
Deliberately: a fake that sounded like a real model would eventually be
mistaken for one in a demo.

### Model and request shape

`claude-opus-5`, `effort: "low"`, no `thinking` parameter, no `budget_tokens`
— which is rejected outright on this model generation. Effort is low because a
desk companion's replies are two sentences and latency is what makes it feel
alive; the depth that `high` buys is not what this workload needs.

Server-side refusal fallbacks are on by default. When a safety classifier
declines, the request routes to another model by category instead of returning
nothing, because a companion going silent on an awkward question reads as
broken rather than principled.

The system prompt is sent as a separate cacheable block and is byte-stable
across turns. Anything varying per turn — the time, the device's state — would
invalidate that prefix on every request, so volatile context belongs in the
messages instead.

## Alternatives considered

**WebSocket for chat too.** One transport for everything. Rejected above: it
solves a problem chat does not have, at the cost of a second authentication
path and worse proxy behaviour.

**Long-polling or a completion endpoint only.** Simplest. Rejected as the
primary path — watching a reply appear is most of what makes a companion feel
present rather than transactional — but kept as
`POST /conversations/{id}/messages` for callers with nobody watching, and
because a synchronous path is far easier to test.

**Mocking the SDK in tests.** The usual approach. Rejected for the
conversation tests: a mock asserts that our fake matches our expectations,
which proves nothing about the provider. The offline provider is a real
implementation of the real interface, so the tests exercise the actual code
path. The Anthropic adapter's own logic — request assembly, error translation
— is tested directly, and its HTTP exchange deliberately is not.

**Structured output for emotion and intent.** The brief has the model emit
`{"emotion": "curious", "action": "look_at_user"}` for the behaviour engine.
Deferred to Phase 6, when there is a device to act on it. Building an intent
pipeline now would mean a schema with no consumer, and streaming JSON to a
chat UI complicates the thing this phase exists to deliver.

## Consequences

- Replies appear as they are written, over plain HTTP, with no socket library
  on the client.
- The whole conversation path is testable, and runnable, with no API key —
  288 tests, no secret in CI, no per-run cost.
- NOVA answers even when its provider is misconfigured, which is the honest
  behaviour for a device that is supposed to keep working offline.
- A partial reply is kept rather than lost, in both the provider-failure and
  client-disconnect cases. Both are tested.
- One module imports the Anthropic SDK. Everything else depends on the
  protocol, so a second provider is an adapter and a config value.
- The offline provider's replies are visibly canned. That is the intended
  cost: a degraded NOVA should be obviously degraded.
- Conversation history sent to the model is bounded by a turn count. Older
  context returns through semantic memory in Phase 5 rather than by raising
  that ceiling.
