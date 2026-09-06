# 006 — Rotating refresh tokens with reuse detection

**Status:** Accepted · **Date:** 2026-09-06

## Context

NOVA's mobile client needs long-lived sessions; asking someone to re-enter a
password daily on a companion app is unacceptable. Long-lived credentials on a
mobile device are also the most likely thing to be stolen, via device
compromise, an insecure backup, or malware.

The hard part is not issuing long-lived credentials. It is detecting that one
has been stolen, given that a stolen token is by construction indistinguishable
from a legitimate one at the moment of use.

## Decision

Short-lived access tokens (15-minute JWTs) plus long-lived opaque refresh
tokens that **rotate on every use** and are grouped into a **family** — all
tokens descended from a single login.

Every refresh revokes the presented token and issues a successor in the same
family. If a token that was already rotated is presented again, that is
evidence someone holds a copy they should not, and the entire family is
revoked.

```
login ──► A
          │ refresh
          ▼
          B          A now revoked
          │
attacker replays A ──► family revoked; B dies too
```

Refresh tokens are 384-bit random strings stored only as SHA-256 digests, with
a unique index so a replayed digest maps to exactly one row. Plain SHA-256
rather than Argon2 is deliberate: the input is full-entropy random data, not a
guessable password, so a slow KDF defends against nothing while costing
latency on every refresh.

Two consequences of the design deserve emphasis:

- The family revocation is **committed before the 401 is raised**. The
  request's unit of work rolls back on exception, so without an explicit
  commit the revocation would be undone by the very error that triggered it —
  reducing reuse detection to a log line with no effect. A test fails if that
  commit is removed.
- **Expiry does not revoke a family.** Ageing out is not evidence of
  compromise, and treating it as such would sign users out for being away over
  a weekend.

## Alternatives considered

**Long-lived access tokens, no refresh.** Simplest. Rejected: a stolen token
is valid until it expires, with no revocation path short of rotating the
signing key and logging out every user.

**Non-rotating refresh tokens.** The common implementation. Revocable, which
is already better than nothing. Rejected: theft is undetectable. An attacker
refreshes indefinitely alongside the legitimate user, and neither the user nor
the server ever learns.

**Rotation without reuse detection.** Rotating alone is a small improvement —
a stolen token is only useful until the real client next refreshes. Rejected:
the failure mode is silent and arbitrary. Whichever party refreshes second
gets an error with no explanation, and the server cannot tell which one was
the attacker.

**Revoke only the replayed token.** Contains nothing: the attacker's successor
token, minted from the replay, keeps working.

**Bind tokens to a device fingerprint.** Useful defence in depth, and
complementary rather than alternative. Deferred: fingerprints change on OS
upgrades and app reinstalls, which produces false logouts. Worth revisiting in
Phase 2 alongside device authentication, where a stable device identity
already exists.

## Consequences

- Token theft becomes detectable and self-limiting rather than silent and
  permanent.
- A detected replay signs out the legitimate user too. That is the intended
  trade: forcing one re-authentication is much cheaper than an attacker with
  indefinite access.
- Refresh costs a database write. Acceptable at a 15-minute cadence.
- `refresh_token_reuse_detected` is logged at WARNING with the user and family
  ID — a genuine security signal worth alerting on in Phase 10.
- Clients must handle a 401 on refresh by returning to login. Documented in
  the API contract.
- Rotating the JWT signing key invalidates access tokens but not refresh
  tokens, since those are database rows rather than signed claims. Users
  recover on their next refresh instead of being signed out.
