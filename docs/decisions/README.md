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

Format: Context, Decision, Alternatives considered, Consequences.

Superseded ADRs are kept, not deleted. A decision that was reversed is more
instructive than one that was never questioned — 001 and 007 together show
what changed and why.
