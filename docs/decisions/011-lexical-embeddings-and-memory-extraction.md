# 011 — Lexical embeddings, and extracting memory after the reply

**Status:** Accepted · **Date:** 2026-09-06

## Context

Phase 5 is what makes NOVA a companion rather than a chat window: it has to
remember things about the person it sits with and bring them back later.
Three questions came out of building it, and each one had an obvious answer
that turned out to be wrong.

1. What produces the embeddings? ADR 002 assumed an `EmbeddingProvider`
   would be filled by the same vendor as chat. **Anthropic has no embeddings
   endpoint** — `client.embeddings` does not exist. So a real embedder is a
   different vendor entirely, which is the situation ADR 002 was written for,
   arriving sooner than expected.
2. When does extraction run? It is a second model call per exchange, and the
   obvious place — inline, before returning — makes every reply slower for
   something the person never sees.
3. Where do retrieved memories go in the prompt? The persona is marked as a
   cacheable prefix (ADR 010); memories change every turn.

## Decision

### A lexical embedder, honestly labelled

`LexicalEmbeddingProvider` is the hashing trick: word and character n-grams,
signed-hashed into 1536 buckets, sublinear term frequency, L2-normalised. It
runs locally, needs no key, and is deterministic.

Be exact about what it buys. Measured on real text:

| Query | Candidate | Cosine similarity |
|---|---|---|
| "I drink coffee every morning before work" | "The owner drinks coffee in the mornings" | **0.50** |
| "I drink coffee every morning before work" | "The deployment pipeline failed" | **0.00** |
| "She is running late" | "She runs late" | **0.55** |

So it genuinely retrieves, and character n-grams carry it across morphology.
What it cannot do is relate "espresso" to "coffee", or know that "my partner"
and "my wife" are the same person. Nothing here has read anything.

The alternative was `OfflineEmbeddingProvider` — hashed whole strings, which
we already had. That would have made the tests pass while retrieving nothing,
which is the exact failure the brief calls out: demonstrating ML with
manufactured results. A lexical embedder is a weaker method described
accurately rather than a strong one implied falsely.

Swapping in a real embedder (Voyage, a hosted alternative, a local
transformer) is a configuration change plus a re-embedding pass. The
`embedding_provider` column on every row exists so that pass is detectable:
vectors from different embedders are not comparable, and a silent provider
change would degrade every future search with nothing to point at.

### Extraction runs after the response, in its own session

`MemoryRecorder` is scheduled as a background task once the reply has been
delivered — `BackgroundTasks` on the JSON path, Starlette's `background=` on
the stream. It opens its own database session, for the same reason
`ChatStreamer` does: the request's session is closed by then.

The cost is one case: a client that disconnects mid-stream may never reach
it. That is the right trade. The partial reply is still persisted by the
streamer, and extracting a memory from a half-sent answer is worse than not
extracting one.

### `ChatRequest.context`, separate from `system`

Retrieved memories go into a **second** system block, after the persona's
cache breakpoint. Concatenating them into `system` would have been one line
of code and would have broken the cacheable prefix on every single turn.
Keeping them separate means a different set of memories costs a cache miss on
itself, not on the persona.

### Everything about extraction is allowed to fail

`MemoryExtractor` never raises. Prose around the JSON, a code fence, a single
object instead of an array, an invented category, a score on a 0-10 scale,
a model that answered the question conversationally instead of extracting
anything — each degrades to remembering nothing. One malformed candidate is
dropped without costing the good ones in the same batch.

This is not defensiveness for its own sake. The input is a language model's
output, and the operation is a background improvement to a reply the person
has already received. There is no failure here worth surfacing to them.

### Sensitive content is filtered twice

The extraction prompt forbids credentials, card numbers, government
identifiers, addresses, and medical details. A pattern check then drops
anything that looks like a secret regardless of what the model did. The
patterns are narrow on purpose — "keeps forgetting their password" is a
legitimate memory and stays, "password is hunter2" does not. A filter that
ate the first would quietly make NOVA worse at the exact subject its owner
works in.

### Read, correct, delete, and forget everything

`GET /memories`, `GET /memories/search`, `PATCH`, `DELETE`, and a `DELETE`
that clears the lot. Editing the text re-embeds it, so a correction reaches
retrieval and not just the screen; skipping that would leave a memory that
reads correctly and is still found by whatever it used to say.

`DELETE /memories` is deliberately separate from deleting the account.
Wanting NOVA to stop knowing things about you is a different intention from
wanting to stop using it.

## Consequences

**Good**

- Retrieval works and is demonstrable end to end, against real Postgres with
  pgvector, with the ownership boundary in the WHERE clause.
- Replies do not slow down as NOVA learns more.
- The persona keeps its cacheable prefix.
- The upgrade path to real embeddings is a setting and a backfill.

**Bad**

- Retrieval quality is capped at shared vocabulary until a real embedder is
  configured. Judging semantic recall needs one; this cannot be assessed with
  what is here.
- Extraction costs a model call per exchange. `NOVA_AI__MEMORY_EXTRACTION_ENABLED`
  turns it off, which is a real trade rather than a free switch.
- The HNSW index does not pay for itself yet. Every search also filters by
  `user_id`, and HNSW applies that filter after traversing the graph, so at
  small per-user counts the planner prefers — and should prefer — an exact
  scan. The index is declared now so the transition needs no migration.
- Deduplication merges on a distance threshold, so two genuinely different
  memories phrased almost identically would collapse into one. The threshold
  is tight (0.12) because that failure is silent, while a near-duplicate
  merely wastes a row.
