# 015 — The phone is the terminal, and the robot is gone

**Status:** Accepted · **Date:** 2026-09-16

## Context

NOVA was a physical desk companion: an ESP32-S3 with an AMOLED face, two
servos for a head that turned, a time-of-flight sensor for presence, and a
backend built to provision it, speak its WebSocket protocol, and do
behavioural analytics on its telemetry.

A fair amount of that was built and working — the firmware core had its own
test suite, the claim flow was solid, the analytics refused to guess. What was
not working was the product question underneath it: *what is this for?*

The honest answer, after living with the design, was that the compelling part
had nothing to do with the hardware. The parts worth keeping were that it
remembered things about its owner and that it was always visible on the desk.
The servos were a demo. The face was a demo. The presence sensor produced
analytics about when somebody sat at their own desk, which is not information
anybody needs.

Meanwhile there was an old iPhone in a drawer: a better screen than the AMOLED,
a better microphone, a speaker, a touch interface, a battery, Wi-Fi, and a
mature SDK. It cannot turn its head. Nothing was ever gained by turning a head.

## Decision

**NOVA is a local-first personal AI terminal. The only hardware is an old
iPhone in a stand, and it is a client.**

Removed entirely: firmware, hardware design, the device tables, provisioning
and claiming, device credentials, the device WebSocket, telemetry ingestion,
behavioural analytics and the insight engine, and the ML pipeline that had
telemetry as its only data source.

Kept: authentication, conversations, streaming, semantic memory, the provider
abstraction, and the SwiftUI client.

Added: a permissioned tool system ([013](017-tool-system-and-permissions.md)),
local model providers, a dashboard, an audit log.

### Concepts were migrated, not deleted

The device concepts had software analogues, and the mapping was the useful
part of the refactor:

| Was | Is | Why it is the same question |
|---|---|---|
| Device telemetry | Tool invocations | "What has this thing been doing?" |
| Device command | Tool invocation | "Make this thing do something" |
| Device status | System status | "Is the thing NOVA watches all right?" |
| Device provisioning | Server address in Settings | "Which NOVA is this?" |
| Device analytics | The audit log | "What happened, and when?" |

`tool_invocations` is `device_telemetry` with a different subject. The
dashboard is the insights screen asking about a machine instead of a robot.
Recognising that is what kept the refactor from being a rewrite.

### The phone does not run the model

It authenticates, renders, streams, and listens. The model runs on the Mac.

This is not a temporary limitation waiting on faster phones. The machine worth
asking about — the one with the repositories, the containers and the services
— is the Mac, and the tools have to run where the thing they inspect is. A
model on the phone would still have to call back to the Mac for everything
interesting, and would be worse at it.

### One address, typed by hand

There is no discovery protocol, no pairing, no QR code. Settings has a text
field for `http://192.168.1.20:8000`.

That is a deliberate downgrade from the old claim flow, which was genuinely
more elegant. It is right because the old flow existed to bind a *device with
no keyboard* to an account; a phone has a keyboard. Everything the claim flow
did — the short code, the provisioning token, the single-use semantics — was
machinery for a constraint that no longer exists.

## Alternatives considered

**Keep the robot and add the terminal.** Two clients, two protocols, and the
hardware still unbuilt. The device code was not free to keep: it was four
tables, a WebSocket protocol, a claim flow, a telemetry pipeline and an
analytics engine that would all need maintaining and migrating for the sake of
something nobody used.

**Keep the tables, drop the firmware.** Tempting because the schema was good.
Rejected: a `devices` table with no devices is a trap for the next person, and
`alembic check` would have quietly kept a dead schema alive forever.

**A web app instead of a native one.** Cheaper, and it would run on anything.
Rejected for the same reasons as [ADR 007](007-native-swiftui-client.md), plus
one new one: an always-on page in Safari on an old phone is a worse experience
than an app in every respect that matters here — backgrounding, audio session,
speech recognition, and Keychain.

**A Mac menu-bar app.** Honestly a good idea, and it would need no network
configuration at all. Rejected because it is not the product: the point is a
second screen that is *already on* and does not compete with what is on the
main one. This remains the most plausible thing to build next.

**Cloud-hosted, phone connects over the internet.** Rejected on the founding
premise. Local-first is not a deployment detail here; it is the reason the
tool system can be trusted with a Docker socket at all.

## Consequences

**Good.** The product is buildable today with hardware already owned. Every
part of the stack that was deferred behind "once the robot exists" shipped
instead. The security model got sharper, because a system that can delete
containers demands more rigour than one that can turn a servo.

**Bad.** The deleted work was real work, and some of it was better than what
replaced it — the claim flow in particular. The ADRs for it are kept and
marked superseded rather than removed, because a decision log that quietly
deletes the decisions that did not last is not a log.

NOVA is also less distinctive. "An AI on a phone" is a crowded description in
a way "a robot that watches your desk" was not. The differentiation now has to
come from the memory and the tools, which is a harder thing to demonstrate in
a photograph.

**Reversible?** Partly. The migration's `downgrade` rebuilds the device
schema exactly, so the tables can come back. The firmware is in git history
and would come back as an archaeology exercise. Nothing about the current
design prevents a device from being added later as *another client* — which is
the right shape for it, and arguably always was.
