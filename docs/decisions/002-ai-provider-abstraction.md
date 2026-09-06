# 002 — Abstract AI providers behind interfaces

**Status:** Accepted · **Date:** 2026-09-06

## Context

NOVA needs five distinct AI capabilities: chat completion, vision, embeddings,
speech-to-text, and text-to-speech. Each has multiple viable providers, and
they differ in cost, latency, quality, and privacy posture.

Two facts drive this decision. First, this market moves faster than the
project will: pricing and model quality change on a timescale of months.
Second, running local models on a home server is a realistic goal for a device
that has a camera and microphone in someone's home — some users will want no
audio or video leaving the house at all.

## Decision

Define narrow interfaces — `ChatProvider`, `VisionProvider`,
`EmbeddingProvider`, `SpeechToTextProvider`, `TextToSpeechProvider` — and
implement adapters per vendor. Business logic depends only on the interfaces.
Configuration selects the implementation at startup.

Interfaces are segregated by capability rather than combined into one
`AIProvider`. A local Whisper deployment implements speech-to-text and nothing
else; forcing it to satisfy a fat interface would mean stub methods that raise.

## Alternatives considered

**Call one vendor's SDK directly.** Simplest, fastest to write, and the right
answer for a project with a fixed provider. Rejected: it puts vendor types in
service signatures, which makes both swapping providers and testing without
network access into refactors rather than configuration changes.

**A framework abstraction (LangChain or similar).** Provides these
abstractions ready-made, plus much more. Rejected: NOVA needs five interfaces
with roughly three methods each. Adopting a large framework to get them means
inheriting its dependency surface, its release cadence, and its opinions about
control flow, in exchange for interfaces that take an afternoon to write. If
NOVA later needs agent orchestration or complex retrieval chains, this can be
revisited.

**Abstract only the LLM.** Pragmatic, since chat is the most volatile piece.
Rejected as inconsistent: speech pricing is at least as volatile, and local
speech models are the most likely privacy-driven substitution.

## Consequences

- Adding a provider is a new adapter and a config value — no business-logic
  change.
- Tests use fake providers, so the suite needs no network and no API keys.
- A local-only deployment is a configuration, not a fork.
- Interfaces expose the intersection of provider capabilities. Vendor-specific
  features need either a deliberate interface extension or an explicit escape
  hatch; this is the real cost of the decision.
- One extra indirection layer to read through when debugging.
