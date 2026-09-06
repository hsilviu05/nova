# Architecture Decision Records

Each ADR records one decision: what was chosen, what was rejected, and what it
costs. The rejected options are the useful part — anyone can see what was
built, but the reasoning disappears unless it is written down.

| # | Decision | Status |
|---|---|---|
| [001](001-mobile-stack.md) | React Native + Expo for the mobile client | Accepted |
| [002](002-ai-provider-abstraction.md) | Abstract AI providers behind interfaces | Accepted |
| [003](003-postgres-pgvector.md) | PostgreSQL + pgvector for memory | Accepted |
| [004](004-websocket-device-protocol.md) | WebSocket, not MQTT, for the device | Accepted |
| [005](005-ml-temporal-split.md) | Temporal splits for behavioural ML | Accepted |
| [006](006-refresh-token-rotation.md) | Rotating refresh tokens with reuse detection | Accepted |

Format: Context, Decision, Alternatives considered, Consequences.
