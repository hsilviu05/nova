# NOVA for iOS

Native SwiftUI client. An old iPhone in a stand on the desk, talking to NOVA
on the Mac beside it.

The phone is a terminal, not a compute node. It authenticates, renders,
streams, and listens; the model and every tool run on the machine under the
desk. See [ADR 019](../../docs/decisions/019-iphone-terminal.md) for why.

## Running it

```bash
cd apps/ios
brew install xcodegen && xcodegen generate
open NOVA.xcodeproj
```

`project.yml` is the source of truth. The `.xcodeproj` is generated, not
committed as an editable artefact: it is large, it conflicts on almost every
merge, and `project.yml` says the same thing in forty readable lines.
Regenerate rather than resolving a conflict in it.

`tools/generate_xcodeproj.py` produces the same project without XcodeGen, for
an environment that has no Swift toolchain at all. It understands only the
subset of `project.yml` this project uses. If the spec outgrows it, delete it
rather than extending it into a bad clone.

The backend must be running:

```bash
docker compose up            # from the repository root
```

### Pointing it at your Mac

Debug builds default to `http://127.0.0.1:8000`, which works **in the
simulator only**. On the phone, `127.0.0.1` is the phone.

In the app: **Settings → NOVA server**, and enter the Mac's address on your
network — `http://192.168.1.20:8000` or `http://your-mac.local:8000`. The
Settings screen says so explicitly while the address is still loopback,
because it is the first thing everyone gets wrong.

Three things about local networking:

- **ATS.** The app declares `NSAllowsLocalNetworking`, which permits cleartext
  to private and link-local addresses and `.local` names. Not
  `NSAllowsArbitraryLoads` — cleartext to the open internet stays blocked, and
  `ServerSettings` refuses to *save* an `http://` address outside those
  ranges rather than saving one that silently never connects.
- **Permission.** iOS asks for local network access on the first attempt.
  Denying it means the app cannot reach your Mac at all.
- **Binding.** The API has to be on `0.0.0.0`, not `127.0.0.1`. Compose does
  this already.

Changing the address signs you out and rebuilds the networking stack: an
account on one NOVA is not an account on another, and a token from one server
rejected by the next is a confusing way to learn that.

### On a desk

**Settings → Display & Brightness → Auto-Lock → Never**, on the charger.

The app does nothing to defeat the lock screen or to keep itself alive in the
background — no background modes, no silent pushes, no keep-alive. The
dashboard polls every fifteen seconds while it is on screen and stops when it
is not, because a timer running in someone's pocket is a battery drain buying
nothing anyone can see.

## Stack

| Concern | Choice |
|---|---|
| UI | SwiftUI |
| State | Observation (`@Observable`) |
| Concurrency | Swift 6, strict checking complete |
| Networking | `URLSession`, hand-rolled SSE |
| Voice | `SFSpeechRecognizer` + `AVSpeechSynthesizer`, behind protocols |
| Tokens | Keychain, `AfterFirstUnlockThisDeviceOnly` |
| Tests | Swift Testing |
| Dependencies | **none** |

Deploying to **iOS 18**, built against the current SDK — independent settings,
see [ADR 007](../../docs/decisions/007-native-swiftui-client.md).

## Layout

```
NOVA/
├── App/            entry point, composition root, root navigation
├── Core/           networking, SSE, coding, Keychain, server settings
├── Models/         API types, mirroring the server contract
└── Features/
    ├── Auth/       sign in and register
    ├── Dashboard/  the glanceable screen: AI, machine, projects, memory, tools
    ├── Chat/       streaming replies, tool activity, confirmations
    ├── History/    past threads, searchable by title or anything said
    ├── Intents/    Siri and Shortcuts: "is SnapWorth up?" without opening the app
    ├── Memory/     what NOVA knows, editable and deletable
    ├── Tools/      what NOVA can do, and doing it by hand
    ├── Voice/      speech in and out, off by default
    └── Settings/   server address, account, voice
```

`AppContainer` is the only place anything is constructed. Views receive what
they need through the environment, which is what makes previews and tests
possible without a server.

## Three details that matter

**Refresh is serialised, and that is not an optimisation.** The API rotates
refresh tokens and revokes the entire family when a rotated one is replayed
([ADR 006](../../docs/decisions/006-refresh-token-rotation.md)). If two
requests both got a 401 and both refreshed, the second would present a token
the first had already rotated — the server would correctly read that as theft
and sign the user out everywhere. `APIClient` is an actor holding a single
in-flight renewal task, so concurrent callers await one attempt.

**The confirmation sheet is the same sheet everywhere.** A destructive action
proposed by NOVA mid-conversation and one started from the Tools screen raise
identical UI, carrying the server's words rather than the app's. The decision
is the same decision, and it should not look different depending on how it was
reached. The app cannot construct a confirmation of its own: the prompt and
the token both come from the server, because only the server knows what the
call would actually do.

**Timestamps need a custom decoder.** The API emits microseconds
(`2026-09-16T15:42:28.579713Z`) and `JSONDecoder`'s built-in `.iso8601`
strategy does not parse fractional seconds, so it would fail on almost every
response. `JSONCoding` accepts both forms, because a timestamp landing exactly
on a whole second serialises without the fractional part.

## Voice

Off until it is turned on, in both directions.

Hold the microphone to talk; releasing puts the transcript **in the composer**
to be read and edited before sending. Nothing is sent by voice without being
seen — a terminal that acts on what it thought it heard is a terminal nobody
trusts with a Docker socket.

`requiresOnDeviceRecognition` is set wherever the device supports it. Without
it, audio goes to Apple's servers, and a terminal whose whole premise is that
nothing leaves the network should not quietly make an exception for the
microphone.

`SpeechRecogniser` and `Speaker` are protocols so a local Whisper on the same
Mac as the model can replace the Apple implementations later — a new file
rather than a refactor.

## Siri and Shortcuts

Two App Intents ship with the app and need no setup in the Shortcuts app:

> "Is SnapWorth up in NOVA?"
> "How is NOVA?"

The first resolves the project name against the ones the server knows, so
"snap" is enough, and reads out the same verdict the dashboard shows: healthy
or not, how fast it answered, and which of its dependencies are down. The
second is the dashboard's status line as one sentence.

They run in the app's own process with the same stored address and session,
reach only the read-only status endpoint, and can change nothing. With no
session stored they say so rather than failing silently. The wording lives in
`StatusPhrasing`, which is where the tests are.

A lock-screen widget is not included yet: it needs a widget extension, an
App Group to share the session with, and Keychain sharing, all of which need
a signing team in the project before they will build. That is a one-line
change once a team is set, and the intents above are the half that does not
need one.

## Testing

```bash
xcodebuild -project NOVA.xcodeproj -scheme NOVA \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
```

No server required. The decoding tests run against payloads captured from a
running API rather than invented, and the model tests feed events in by hand —
what is under test is how a transcript assembles, not how it arrived.

`scripts/check_ios_contract.py` at the repository root compares every model
here against the server's OpenAPI schema, including the enums this app mirrors
from server literals. It runs on Linux in CI, so a contract break is caught on
every push rather than only where a macOS runner is available.

## Privacy

`PrivacyInfo.xcprivacy` declares email, name, and product interaction, none of
it used for tracking. No camera is used at all.

`NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription` and
`NSLocalNetworkUsageDescription` are all present and all say what is actually
done with the permission.

## Known gaps

- **Tool activity is not persisted in the transcript.** Reopening a thread
  shows what was said, without the machinery. The audit log under
  Dashboard → recent activity is where "what did NOVA run" is answered
  permanently.
- **Notifications only while the app is open.** The server's watcher raises
  alerts; the dashboard polls for them and raises a local banner for each new
  one, including in the foreground. Reaching a closed app is APNs, which
  needs an Apple key, an entitlement and a signing team. Alerts already on
  the server when the app opens are shown on the dashboard but not announced:
  history, not news.
- **One server at a time.** Switching is a Settings change and a sign-out.
