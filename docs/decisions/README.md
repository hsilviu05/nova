# Architecture Decision Records

Each ADR records one decision: what was chosen, what was rejected, and what it
costs. The rejected options are the useful part — anyone can see what was
built, but the reasoning disappears unless it is written down.

| # | Decision | Status |
|---|---|---|
| [001](001-mobile-stack.md) | React Native + Expo for the mobile client | ~~Superseded by 007~~ |
| [002](002-ai-provider-abstraction.md) | Abstract AI providers behind interfaces | Accepted |
| [003](003-postgres-pgvector.md) | PostgreSQL + pgvector for memory | Accepted |
| [004](004-websocket-device-protocol.md) | WebSocket, not MQTT, for the device | Accepted |
| [005](005-ml-temporal-split.md) | Temporal splits for behavioural ML | Accepted |
| [006](006-refresh-token-rotation.md) | Rotating refresh tokens with reuse detection | Accepted |
| [007](007-native-swiftui-client.md) | Native SwiftUI for the mobile client | Accepted |
| [008](008-amoled-face-hardware.md) | An AMOLED face instead of a camera | Accepted |
| [009](009-device-claim-flow.md) | Device claiming by on-screen code | Accepted |
| [010](010-streaming-and-offline-ai.md) | Streaming chat over SSE, with an offline provider | Accepted |
| [011](011-lexical-embeddings-and-memory-extraction.md) | Lexical embeddings, and extracting memory after the reply | Accepted |
| [012](012-analytics-in-sql-and-gated-insights.md) | Aggregation in SQL, and insights that refuse to guess | Accepted |
| [013](013-ml-pipeline-guarantees.md) | The ML pipeline's guarantees are code, not conventions | Accepted |
| [014](014-local-llm-via-ollama.md) | A local model through Ollama, natively on the host | Accepted |
| [015](015-github-dev-mode.md) | GitHub dev mode: a public webhook, and what it may and may not do | Accepted |
| [016](016-production-posture.md) | Production posture: refuse to boot, one host, nothing published but the edge | Accepted |

Format: Context, Decision, Alternatives considered, Consequences.

Superseded ADRs are kept, not deleted. A decision that was reversed is more
instructive than one that was never questioned — 001 and 007 together show
what changed and why.
