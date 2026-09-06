# 009 — Device claiming by on-screen code

**Status:** Accepted · **Date:** 2026-09-06

## Context

A NOVA has to end up bound to exactly one account, and the device has to end
up holding a credential it can authenticate with. The hard part is that at
first boot the device has no credential and the user has no way to identify it
— so something has to bootstrap trust across that gap.

Whatever bootstraps it must not make **physical presence** and **account
ownership** the same thing by accident. Someone standing at the desk should be
able to adopt the device onto their account; that is what a factory reset
means. They should not thereby be able to impersonate the device to the
backend afterwards.

## Decision

A **claim flow keyed on a code the device displays**, with two independent
secrets.

```
1. Device boots unprovisioned
   POST /devices/provision {hardware_id, model, firmware_version}
   -> {claim_code: "P52-HAS", provisioning_token, expires_at}
   The device renders P52-HAS on its AMOLED face.

2. The owner types the code into the iOS app
   POST /devices/claim {code, name}          [user JWT]
   -> the device is bound to that account

3. The device polls with its provisioning token
   POST /devices/provision/poll {provisioning_token}
   -> {status: "pending"} ... then {status: "claimed", device_token}

4. The device connects
   WS /devices/ws   Authorization: Bearer novad_...
```

**The two secrets do different jobs, and that separation is the design.**

| | Claim code | Provisioning token |
|---|---|---|
| Entropy | ~29 bits | 256 bits |
| Displayed | Yes, on the device's face | Never |
| Typed by a human | Yes | No |
| Grants | Binding the device to *your* account | Collecting the device's credential |

Someone who reads the code off the screen can adopt the device. They cannot
obtain the provisioning token, so they cannot collect the credential and
cannot speak *as* the device. The claim response is asserted, in tests and in
the simulator, never to contain the device token.

Supporting choices:

- **The code is short and forgiving.** Six characters from a 29-symbol
  alphabet with `I`, `L`, `O`, `U`, `0` and `1` removed, rendered `P52-HAS`.
  Input is upper-cased and stripped of spaces and hyphens, because people type
  `p52has`.
- **Ten-minute TTL, single use.** The entropy is low by necessity, so the
  defences are time and uniqueness rather than length.
- **Rate limiting is the anti-guessing mechanism**, per user on claim and per
  address on provision. There is deliberately no per-claim attempt counter: a
  wrong guess is looked up by digest, matches no row, and therefore could not
  be attributed to the code it was aiming at. A counter would look like a
  defence while defending nothing.
- **The credential is issued exactly once.** A second successful collection is
  refused, because a repeat means either a device bug or a stolen provisioning
  token.
- **`hardware_id` is an identifier, never a credential.** It is printed on the
  chip and trivially spoofed, so it decides *which row* is provisioned and
  never *who owns it*.
- **Re-provisioning transfers ownership.** Physical possession is treated as
  authority to reset, as on consumer hardware. Re-provisioning revokes the
  device's credentials and clears its owner, so a resold or recovered NOVA
  stops reporting to whoever had it last.

## Alternatives considered

**A secret flashed at build time.** Simplest: burn a per-device secret into
the firmware image and register it out of band. Rejected as a dead end.
Every device needs a distinct image, provisioning cannot happen after
manufacture, a lost secret bricks the unit, and there is no ownership story at
all — the secret would have to be manually associated with an account.
Acceptable for exactly one hand-built device, which is not what the
architecture is being built for.

**Trust the hardware ID.** Let the device present its MAC and be adopted on
that basis. Rejected outright: a MAC is public and spoofable, so anyone who
learned it could claim someone else's device or impersonate it.

**BLE provisioning.** The app talks to the device over Bluetooth and hands it
WiFi credentials and a token. The best possible UX, and the eventual answer
for WiFi setup. Rejected for this phase as disproportionate: it needs a BLE
stack in the firmware, a CoreBluetooth flow in the app, and its own pairing
security model — a phase of work on its own, to solve a problem the screen
already solves. Worth revisiting when WiFi credentials need to reach the
device, since that is a problem a code on a screen cannot solve.

**A QR code on the screen instead of a typed code.** Strictly better UX and a
natural upgrade: the same claim code, transported by camera rather than
fingers. Deferred only because it needs the iOS camera flow that Phase 3 has
not built yet. The backend needs no change to support it, which is the point
of putting the code in the payload rather than the transport.

**A single secret for both jobs.** Display one long token and have the user
transfer it somehow. Rejected: anything short enough to type is too weak to be
a device credential, and anything strong enough to be a credential is too long
to type. The two-secret split exists precisely because one value cannot be
both.

## Consequences

- Setup is the familiar consumer flow — read a code, type it, done — with no
  out-of-band registration step.
- Reading the code grants adoption and nothing more. This is asserted by
  tests, not just intended.
- Losing the provisioning token before collection is recoverable: provision
  again and a new code appears. Losing the device token after collection means
  re-provisioning, which is the correct outcome.
- The unauthenticated provisioning endpoint is an attack surface, mitigated by
  a tight per-address rate limit and by the fact that a provisioned but
  unclaimed device row grants nobody anything.
- Ownership transfer on re-provisioning means anyone with physical access can
  take a device off its owner's account. That is the intended trade, matching
  how consumer hardware behaves, and it is why the previous credentials are
  revoked rather than left live.
- A QR code can be added later with no backend change.
