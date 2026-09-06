# 007 — Native SwiftUI for the mobile client

**Status:** Accepted · **Date:** 2026-09-06 · **Supersedes:** [001](001-mobile-stack.md)

## Context

[ADR 001](001-mobile-stack.md) chose React Native with Expo. That decision
rested on an assumption about the developer that turned out to be wrong.

ADR 001 reasoned that two native apps would be "roughly double the client work
for a single developer" and that the time would come out of the firmware and
ML phases. That argument holds for a generalist who would be learning Swift to
write the client. It does not hold here: the author is a working iOS developer
whose other production project is an App Store Connect analytics platform.
Swift is not a fifth language to learn — it is the language they are fastest
in.

Once that is corrected, ADR 001's central trade-off inverts. React Native is
no longer the cheaper path to a good client; it is a layer of indirection
between the author and the platform they already know.

Two further points reinforce it:

- The product brief asks for "a polished Apple-inspired application." Native
  SwiftUI is the shortest path to that, not an approximation of it.
- NOVA's screens are unusually well served by first-party frameworks. Swift
  Charts covers the entire Insights screen, and no React Native charting
  library is close in quality. `URLSessionWebSocketTask` covers the device
  connection. Keychain covers token storage. AVFoundation and Speech cover
  voice input. The client needs approximately zero third-party dependencies.

## Decision

**Native iOS, SwiftUI, Swift 6.2, iOS 18 deployment target.**

| Concern | Choice |
|---|---|
| UI | SwiftUI |
| State | Observation framework (`@Observable`) |
| Concurrency | Swift Concurrency, strict concurrency checking on |
| Networking | `URLSession` + `URLSessionWebSocketTask` |
| Charts | Swift Charts |
| Token storage | Keychain, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` |
| Dependencies | None planned |

Build with the **iOS 26 SDK** (Xcode 26.2+), which App Store Connect has
required for all uploads since 28 April 2026, while **deploying to iOS 18**.
Build SDK and deployment target are independent, and conflating them is a
common source of needlessly narrow device support. iOS 18 is two years of
devices and already provides everything above; `@Observable` needs 17+ and
Swift Charts needs 16+, so nothing in the design is gated by the floor.

**Android is dropped, not deferred.** Calling it "later" would be dishonest:
nothing in a SwiftUI codebase carries over, so Android would be a new client
written from scratch. If it is ever needed, the REST and WebSocket API is the
contract, and that is deliberately the only coupling.

## Alternatives considered

**Keep React Native + Expo (ADR 001).** Retains Android and one codebase.
Rejected on the corrected premise: for this author it is slower to write,
worse to look at, and it trades away the strongest first-party tooling in the
stack — Swift Charts especially — for a platform that is not needed.

**Kotlin Multiplatform with a SwiftUI front end.** Shares business logic while
keeping native UI, and would preserve an Android path. Rejected as
disproportionate: the client's business logic is an API wrapper and a token
store. Sharing that costs a Gradle toolchain and a KMP build for a few hundred
lines.

**A web app or PWA.** Cheapest to build and reachable from anything. Rejected:
no Keychain, degraded background WebSocket behaviour, and it forfeits the
"polished Apple-inspired" goal, which is the whole point of the client.

**SwiftUI targeting iOS 26 only.** Would allow the newest SwiftUI APIs.
Rejected as a free cost: nothing in the design needs them, and it would
exclude perfectly capable devices for no gain.

## Consequences

- The client is written in the author's strongest language, which raises the
  realistic ceiling on polish — the dimension the product is actually judged
  on.
- Swift Charts makes the Insights screen a first-party feature rather than a
  dependency-management problem.
- Zero third-party client dependencies: nothing to audit, no supply-chain
  surface, no SDK upgrade treadmill.
- iOS-only. Stated plainly in the README rather than left implied.
- CI needs a macOS runner for the client, which is billed at a higher rate
  than Linux. The backend, firmware, and ML jobs stay on Linux, so this
  applies to one job.
- App Store submission, if it ever happens, brings obligations the backend
  does not: a `PrivacyInfo.xcprivacy` manifest, App Privacy nutrition labels,
  and purpose strings for every permission — at minimum
  `NSMicrophoneUsageDescription` for voice input,
  `NSSpeechRecognitionUsageDescription` if on-device transcription is used,
  and `NSLocalNetworkUsageDescription` if the app ever reaches the device
  directly over the LAN rather than through the backend. These are Phase 3
  design inputs, not Phase 10 paperwork.
- **The backend is unchanged.** No API, schema, or protocol change follows
  from this decision, which is the payoff for having kept the client behind an
  HTTP and WebSocket contract from the start.
