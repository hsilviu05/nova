# 003 — PostgreSQL with pgvector for semantic memory

**Status:** Accepted · **Date:** 2026-09-06

## Context

NOVA's memory system stores extracted facts with embeddings and retrieves them
by semantic similarity before answering. Memories also carry relational
structure: they belong to a user, derive from a conversation, and have a
category, importance score, and confidence.

Retrieval is almost never pure vector search. The real query is "the most
similar memories **belonging to this user**, above an importance threshold,
excluding deleted ones" — similarity plus relational filters.

Realistic scale is thousands to low tens of thousands of memories per user.

## Decision

**PostgreSQL with the pgvector extension**, for both relational data and
embeddings.

The extension is enabled in the very first migration, well before any column
uses it. Creating an extension is a database-wide privileged operation; doing
it up front means the Phase 5 migration that adds an embedding column is an
ordinary column addition rather than a privileged one.

## Alternatives considered

**A dedicated vector database (Pinecone, Weaviate, Qdrant, Milvus).** Better
pure-vector performance at large scale, with purpose-built indexing. Rejected
on two grounds. Operationally it is a second datastore to run, back up,
secure, and keep consistent with Postgres — for a single-developer project
that is real recurring cost. Functionally it splits the query: filtering by
user and importance would mean either over-fetching from the vector store and
filtering in the application, or duplicating relational metadata into it. At
NOVA's scale the performance advantage does not pay for either.

**SQLite with a vector extension.** Attractive for a small deployment.
Rejected: the backend is a network service with concurrent writers from the
device, the mobile app, and background jobs. SQLite's write model fits that
badly, and it would foreclose the Postgres-specific features already in use
(`INET`, partial indexes, `ON DELETE CASCADE` semantics).

**Embeddings in a column, similarity in Python.** No extension needed.
Rejected: it means loading every candidate row into the application on every
query. Workable at a hundred memories, not at ten thousand, and it discards
the index entirely.

**Elasticsearch / OpenSearch.** Strong hybrid search. Rejected as heavier to
operate than the problem warrants, for capability NOVA does not currently need.

## Consequences

- One datastore. One backup strategy, one connection pool, one set of
  credentials.
- Similarity and relational filters live in a single SQL query, with the
  planner choosing the strategy.
- Transactional consistency between a memory and its embedding is free —
  they are the same row.
- pgvector's index types (IVFFlat, HNSW) require tuning at scale and are less
  mature than a dedicated engine's. Accepted at this scale.
- Very large deployments might eventually outgrow this. The repository layer
  is where that change would land, and it is one file.
