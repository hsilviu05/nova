# NOVA for iOS

Native SwiftUI client. **Phase 3.**

## Stack

| Concern | Choice |
|---|---|
| UI | SwiftUI |
| State | Observation framework (`@Observable`) |
| Concurrency | Swift Concurrency, strict checking on |
| Networking | `URLSession` + `URLSessionWebSocketTask` |
| Charts | Swift Charts |
| Tokens | Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`) |
| Dependencies | None planned |

Built with the iOS 26 SDK (Xcode 26.2+), deployment target **iOS 18**. Those
are independent settings: App Store Connect requires the former, and the
latter is chosen to be no narrower than the design needs.

## Screens

| Screen | Contents |
|---|---|
| Home | Device status, current mood, battery, WiFi, last interaction |
| Chat | Streaming conversation, voice input, listening/thinking/speaking state |
| Insights | Interaction analytics and prediction, via Swift Charts |
| Memory | View, edit, and delete what NOVA remembers |
| Device | Telemetry, controls, personality configuration |

## Privacy

Purpose strings and a `PrivacyInfo.xcprivacy` manifest are Phase 3 design
inputs, not a submission afterthought. At minimum:

- `NSMicrophoneUsageDescription` — voice input
- `NSSpeechRecognitionUsageDescription` — if transcription runs on device
- `NSLocalNetworkUsageDescription` — only if the app reaches the ESP32
  directly over the LAN rather than through the backend

Nothing here needs camera access: the camera lives on the device, and frames
reach the app through the API.

## Why native rather than React Native

See [ADR 007](../../docs/decisions/007-native-swiftui-client.md), which
supersedes [ADR 001](../../docs/decisions/001-mobile-stack.md).

**iOS only.** Android is dropped rather than deferred — nothing in a SwiftUI
codebase would carry over. The REST and WebSocket API is the contract if
another client is ever written.
