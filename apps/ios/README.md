# NOVA for iOS

Native SwiftUI client. **Phase 3.**

> ### ⚠️ Not yet compiled
>
> Every other part of NOVA was written and verified in a Linux container.
> Swift cannot be. This code has **never been through a compiler** — there is
> no Swift toolchain available in that environment, and SwiftUI does not build
> on Linux regardless.
>
> Expect to fix compile errors on the first build. What *is* verified is the
> part most likely to be wrong silently: every model and every test fixture
> here was written against JSON captured from a running NOVA API, not from
> memory.

## Running it

`NOVA.xcodeproj` is committed, so this is the whole of it:

```bash
cd apps/ios
open NOVA.xcodeproj
```

`project.yml` is still the source of truth. The project is generated from it,
two ways, and both produce the same thing:

```bash
xcodegen generate                        # brew install xcodegen
python3 tools/generate_xcodeproj.py      # no Swift toolchain needed
```

Regenerate rather than resolve a merge conflict in the project file. Both
generators are deterministic for a given spec, so the output is stable and a
diff shows real changes rather than churn.

`tools/generate_xcodeproj.py` exists because NOVA is developed mostly in a
Linux container where there is no Swift toolchain and therefore no XcodeGen.
It understands only the subset of `project.yml` this project uses — two
targets, Swift sources, one resource, build settings. It is not an XcodeGen
replacement, and if the spec grows past what it handles it should be deleted
rather than extended into a bad clone.

> The generated project has been parsed back and checked — every source file
> on disk is referenced, no reference dangles, both targets carry the right
> product types and configurations, and the test target depends on the app.
> It has **not** been opened in Xcode, because there is no Mac here. If it
> refuses to open, `xcodegen generate` overwrites it and is authoritative.

The backend must be running:

```bash
docker compose up            # from the repository root
```

Debug builds point at `http://127.0.0.1:8000`, set in `project.yml` and
reachable from the simulator. An ATS exception covers localhost only — there
is deliberately no blanket `NSAllowsArbitraryLoads`, so a release build must
use HTTPS.

No device to hand? `python scripts/simulate_device.py` runs a fake one that
provisions, prints a claim code, and streams telemetry — enough to exercise
the whole app.

## Stack

| Concern | Choice |
|---|---|
| UI | SwiftUI |
| State | Observation (`@Observable`) |
| Concurrency | Swift 6.2, strict checking complete |
| Networking | `URLSession` |
| Tokens | Keychain, `AfterFirstUnlockThisDeviceOnly` |
| Tests | Swift Testing |
| Dependencies | **none** |

Built against the iOS 26 SDK (required by App Store Connect since April 2026),
deploying to **iOS 18**. Those are independent settings — see
[ADR 007](../../docs/decisions/007-native-swiftui-client.md).

## Layout

```
NOVA/
├── App/           entry point, composition root, root navigation
├── Core/          networking, coding, Keychain, session state
├── Models/        API types, mirroring the server contract
└── Features/
    ├── Auth/      sign in and register
    ├── Home/      device status and vitals
    ├── Devices/   list, detail, claim
    └── Settings/  account, sign out
```

`AppContainer` is the only place anything is constructed. Views receive what
they need through the environment, which is what makes previews and tests
possible without a server.

## Two details that matter

**Refresh is serialised, and that is not an optimisation.** The API rotates
refresh tokens and revokes the entire family when a rotated one is replayed
([ADR 006](../../docs/decisions/006-refresh-token-rotation.md)). If two
requests both got a 401 and both refreshed, the second would present a token
the first had already rotated — the server would correctly read that as theft
and sign the user out everywhere. `APIClient` is an actor holding a single
in-flight renewal task, so concurrent callers await one attempt.

**Timestamps need a custom decoder.** The API emits microseconds
(`2026-09-06T15:42:28.579713Z`) and `JSONDecoder`'s built-in `.iso8601`
strategy does not parse fractional seconds, so it would fail on almost every
response. `JSONCoding` accepts both forms, because a timestamp landing exactly
on a whole second serialises without the fractional part.

## What Phase 3 covers

- Register, sign in, sign out, session restore at launch
- Device list, detail, rename, removal
- **Claiming by typed code** — the flow from
  [ADR 009](../../docs/decisions/009-device-claim-flow.md)
- Home screen: status, vitals folded out of the telemetry stream
- Sending head and expression commands to a connected device

Chat, memory, and insights are Phases 4, 5 and 7. They are not stubbed here —
an empty tab promises something that does not exist.

## Privacy

`PrivacyInfo.xcprivacy` declares email, name, and product interaction, none of
it used for tracking. No camera string is needed: the device has no camera
([ADR 008](../../docs/decisions/008-amoled-face-hardware.md)).

`NSMicrophoneUsageDescription` and `NSSpeechRecognitionUsageDescription`
become necessary in Phase 4, when voice input arrives.

## Known gaps

- **Never compiled.** See the notice above.
- **No WebSocket client yet.** The app polls on appear and pull-to-refresh.
  Live push is Phase 4, alongside streaming chat.
- **Single device assumed.** The home screen shows the first claimed device.
  The list handles several; the model does not need changing to support more.
- **No UI tests.** They need a simulator to be meaningful.
