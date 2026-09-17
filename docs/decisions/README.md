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
| [012](012-analytics-in-sql-and-gated-insights.md) | Aggregation in SQL, and insights that refuse to guess | ~~Superseded by 019~~ |
| [013](013-ml-pipeline-guarantees.md) | The ML pipeline's guarantees are code, not conventions | Accepted · dormant since 019 |
| [014](014-local-llm-via-ollama.md) | A local model through Ollama, natively on the host | Accepted · amended by 019 |
| [015](015-github-dev-mode.md) | GitHub dev mode: a public webhook, and what it may and may not do | Accepted · amended by 019 |
| [016](016-production-posture.md) | Production posture: refuse to boot, one host, nothing published but the edge | Accepted |
| [017](017-tool-system-and-permissions.md) | A permissioned tool system, and confirmation as the boundary | Accepted |
| [018](018-treating-tool-output-as-hostile.md) | Treating tool output as hostile input | Accepted |
| [019](019-iphone-terminal.md) | The phone is the terminal, and the robot is gone | Accepted |
| [020](020-embedding-width-is-a-ceiling.md) | The embedding column is a ceiling, and a switch is a re-embedding pass | Accepted |

Format: Context, Decision, Alternatives considered, Consequences.

Superseded ADRs are kept, not deleted. A decision that was reversed is more
instructive than one that was never questioned.

Five of these were superseded at once, on 2026-09-16, when NOVA stopped being
a physical desk companion and became a phone-first terminal — see
[019](019-iphone-terminal.md). Each carries a note saying what replaced it and,
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

Three more sit in between, written just before the pivot and outlived by it
in different degrees. [013](013-ml-pipeline-guarantees.md) is **dormant**: the
package and its tests are intact and its guarantees still hold, but its only
data source was device telemetry, so nothing can currently produce the CSV it
reads. [014](014-local-llm-via-ollama.md) was **right and arrived first** —
the local-model decision was already made before the terminal needed one; what
changed is that tool calling became a requirement of the model rather than a
nice-to-have. [015](015-github-dev-mode.md) kept its whole input half —
signed webhook, replay defence, the refusal ladder — and lost its output half,
which pushed a face to a robot.
