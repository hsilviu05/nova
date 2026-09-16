# Architecture Decision Records

Each ADR records one decision: what was chosen, what was rejected, and what it
costs. The rejected options are the useful part — anyone can see what was
built, but the reasoning disappears unless it is written down.

| # | Decision | Status |
|---|---|---|
| [001](001-mobile-stack.md) | React Native + Expo for the mobile client | ~~Superseded by 007~~ |
| [002](002-ai-provider-abstraction.md) | Abstract AI providers behind interfaces | Accepted · amended 2026-09-16 |
| [003](003-postgres-pgvector.md) | PostgreSQL + pgvector for memory | Accepted |
| [004](004-websocket-device-protocol.md) | WebSocket, not MQTT, for the device | ~~Superseded by 013~~ |
| [005](005-ml-temporal-split.md) | Temporal splits for behavioural ML | ~~Superseded by 015~~ |
| [006](006-refresh-token-rotation.md) | Rotating refresh tokens with reuse detection | Accepted |
| [007](007-native-swiftui-client.md) | Native SwiftUI for the mobile client | Accepted |
| [008](008-amoled-face-hardware.md) | An AMOLED face instead of a camera | ~~Superseded by 015~~ |
| [009](009-device-claim-flow.md) | Device claiming by on-screen code | ~~Superseded by 015~~ |
| [010](010-streaming-and-offline-ai.md) | Streaming chat over SSE, with an offline provider | Accepted · amended 2026-09-16 |
| [011](011-lexical-embeddings-and-memory-extraction.md) | Lexical embeddings, and extracting memory after the reply | Accepted |
| [012](012-analytics-in-sql-and-gated-insights.md) | Aggregation in SQL, and insights that refuse to guess | ~~Superseded by 015~~ |
| [013](013-tool-system-and-permissions.md) | A permissioned tool system, and confirmation as the boundary | Accepted |
| [014](014-treating-tool-output-as-hostile.md) | Treating tool output as hostile input | Accepted |
| [015](015-iphone-terminal.md) | The phone is the terminal, and the robot is gone | Accepted |

Format: Context, Decision, Alternatives considered, Consequences.

Superseded ADRs are kept, not deleted. A decision that was reversed is more
instructive than one that was never questioned.

Five of these were superseded at once, on 2026-09-16, when NOVA stopped being
a physical desk companion and became a phone-first terminal — see
[015](015-iphone-terminal.md). Each carries a note saying what replaced it and,
where something survived the change, what that was. The claim flow in
[009](009-device-claim-flow.md) is the one most worth reading: the mechanism
is gone, and the reasoning about separating a human-transcribable secret from
a machine one is exactly the reasoning behind the confirmation tokens that
replaced it.

Two of the accepted ones carry amendments rather than replacements.
[002](002-ai-provider-abstraction.md) records what the provider abstraction
actually bought when the project moved from cloud-first to local-first — two
new files — and the one prediction in it that did not hold.
[010](010-streaming-and-offline-ai.md) records that SSE survived and the
argument for it got *stronger*, for a reason nobody anticipated when it was
chosen.
