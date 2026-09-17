# 020 — The embedding column is a ceiling, and a switch is a re-embedding pass

**Status:** Accepted · **Date:** 2026-09-17 · Amends 011 and 014

## Context

The memories column is `vector(1536)`, and the width is baked into the
schema because pgvector's index needs it. The lexical embedder fills it.
The models worth switching to do not: nomic-embed-text produces 768
dimensions, mxbai-embed-large 1024. ADR 014 deferred the question of how
a real embedder would fit until there were real memories to re-embed.
There are now, and the deferred question had two parts: how a narrower
model fits the column, and what happens to the rows already in it.

## Decision

### The width is a ceiling. Narrower vectors are zero-padded.

Cosine distance is unchanged by appending zeros to both vectors: the dot
product gains only zero terms, and so do both squared norms. Every angle
the index compares is exactly the angle the model produced. So a 768-wide
model writes 768 numbers followed by 768 zeros, and retrieval is exact.

The earlier registry docstring said padding "would silently destroy the
geometry". That was half right: *truncation* destroys it, and truncation
is still refused. A model wider than the column fails at startup with the
two honest options named — a narrower model, or a migration.

The cost is storage: a 768-wide vector occupies 1536 floats. For a
personal store of hundreds to thousands of memories that is kilobytes,
and it buys not having a migration every time the model changes.

### The provider name carries the model, and retrieval filters on it.

Vectors from two embedders are not comparable, whatever their widths.
Each memory row already recorded which provider wrote it; it now records
the model too (`openai_compatible:nomic-embed-text`), because two models
behind one endpoint are two spaces.

Retrieval compares only rows written by the current embedder. A row from
another is not ranked wrongly; it is left out, counted as stale on the
dashboard, and named in a warning at startup. This is the safe failure —
NOVA forgets things until the pass runs, rather than recalling the wrong
things with confidence — and it is a *loud* safe failure.

### The switch is one command, and it is all-or-nothing.

`scripts/reembed_memories.py` rewrites every stale row in the new space
inside one transaction. It probes the embedder before reading a row, so
an unreachable model server fails in a second rather than after an hour,
and a server that dies halfway leaves the store exactly as it was. There
is a dry run.

## Alternatives considered

- **Make the column width configurable.** The width would then have to
  agree between the model definition, the migration, and every
  environment's `.env`, and a disagreement would show up as schema drift
  or an index that will not build. Padding makes the width a property of
  the schema alone.
- **Re-embed lazily on read.** Retrieval would then call the embedding
  model on the request path, which is the one place it must not be.
- **Migrate the column to the new model's width each time.** A migration
  per model choice, each destroying the index and rebuilding it, to save
  kilobytes.
- **Keep comparing across providers and hope.** Distances between spaces
  are noise. Retrieval would degrade to random with no signal that it had.

## Consequences

- Any embedding model up to 1536 wide works with a configuration change
  and one command. Wider needs a migration, and says so.
- Existing lexical rows are untouched by any of this: the built-in
  embedders always fill the column, and `embedding_dimensions` describes
  only the external model.
- The dashboard's memory card has two new fields: which embedder is
  current, and how many memories it cannot see. A non-zero count is the
  instruction to run the pass.
- Switching back is the same command in the other direction.
