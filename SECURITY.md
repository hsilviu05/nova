# Security

NOVA sits on a desk with a microphone pointed at its owner. That sets the bar.

## Reporting a vulnerability

Open a private security advisory on the repository. Please do not open a
public issue for an exploitable defect.

## What is implemented today (Phase 1)

### Password storage

**Argon2id**, the OWASP first-choice password KDF. Memory-hard, so GPU and
ASIC attackers gain far less than they do against SHA-family hashes. Defaults
follow the OWASP baseline (19 MiB, 2 iterations, 1 lane) and are configurable
per environment.

Hashes are upgraded opportunistically: when a user logs in successfully and
their stored hash predates the current work factors, it is rehashed. Raising
the cost parameters therefore migrates the user base over time instead of
requiring a password reset.

Passwords are bounded at 128 characters. Argon2's cost grows with input
length, so an unbounded password field is a cheap denial-of-service vector.

### Access tokens

15-minute JWTs, HMAC-signed. At verification the algorithm is **pinned** to
the configured one; the token's own `alg` header is never consulted. Trusting
it is the classic JWT confusion attack, and there is a test that forges an
`alg: none` token specifically to prove the door is shut.

Issuer, audience, expiry, and a `typ: access` claim are all required and
checked. A token of any other class is rejected even when correctly signed.

Authentication does not stop at the signature: the subject is loaded from the
database and checked for existence and active status on every request. That is
what makes deactivating an account take effect immediately rather than
whenever the outstanding token happens to expire.

### Refresh tokens

384 bits of `secrets.token_urlsafe`, stored **only** as a SHA-256 digest. A
database leak yields digests, not usable tokens.

SHA-256 rather than Argon2 is deliberate. The input is full-entropy random
data, not a guessable human password, so there is nothing for a slow KDF to
defend against — and a fast digest keeps per-request lookup cheap.

They are opaque rather than JWTs because a refresh token must be revocable,
and a stateless JWT cannot be revoked before it expires.

### Rotation and reuse detection

Every refresh rotates: the presented token is revoked and a successor is
issued in the same **family** — the set of all tokens descended from one
login.

```
login ──► A
          │ refresh
          ▼
          B          A revoked
          │
attacker replays A ──► whole family revoked; B dies too
```

Replaying a retired token means someone holds a copy they should not. The
response is to revoke the entire family, which forces both parties to
re-authenticate. That is the correct outcome: the alternative is an attacker
refreshing quietly and indefinitely.

Two implementation details this depends on:

- The revocation is **committed before the 401 is raised**. The request's
  unit of work rolls back on exception, so without an explicit commit the
  revocation would be undone by the very error it triggered. A test fails if
  that commit is removed.
- `token_hash` carries a **unique index**, so a replayed digest maps to
  exactly one row.

Expiry is treated differently from replay: a token that merely aged out does
*not* revoke its family. Expiry is not evidence of compromise, and treating it
as such would sign users out for being away over a weekend.

### Account enumeration

An unknown email and a wrong password produce byte-identical responses: same
status, same error code, same message. Login also runs a dummy Argon2 verify
when the email is unknown, so the two cases cost the same wall-clock time —
otherwise response latency alone reveals which emails have accounts.

Logout returns 204 for unknown tokens for the same reason.

### Rate limiting

Fixed-window counters in Redis on `register`, `login`, and `refresh`, keyed by
client address. Fixed window rather than a sliding log: one `INCR` and a
conditional `EXPIRE` per request, with no per-request member set to trim. The
trade-off — a burst of up to 2× the limit across a window boundary — is
acceptable for slowing credential stuffing.

The limiter **fails open**. If Redis is unreachable it logs a warning and
allows the request. Losing the cache should degrade abuse protection, not take
authentication down.

### Information disclosure

- Unhandled exceptions log their detail and return a generic message.
  Internal errors disclose schema names, file paths, and sometimes
  credentials.
- Validation errors return only `type`, `loc`, and `msg`. Pydantic's `input`
  field is dropped **specifically** because a failing password rule would
  otherwise reflect the submitted password back to the caller, and into any
  log or proxy that records response bodies.
- OpenAPI docs and the schema endpoint are disabled in production.
- Incoming `X-Request-ID` headers are not trusted by default; accepting one
  would let a caller inject arbitrary text into log fields.

### Transport and CORS

CORS defaults to **no allowed origins**, which is correct for a native mobile
client — it makes no preflighted cross-origin requests. Origins are added
explicitly only when a browser front-end needs them. Methods and headers are
enumerated rather than wildcarded.

### Container

The API image runs as a non-root user (uid 10001). The build toolchain lives
in a separate builder stage and never reaches the runtime image.

### Device credentials

A device is bound to an account by the claim flow in
[ADR 009](docs/decisions/009-device-claim-flow.md), which uses two secrets
with deliberately different jobs.

The **claim code** is shown on the device's face and typed by the owner. At
roughly 29 bits it is weak by necessity, so it lives ten minutes, works once,
and the claim endpoint is rate limited per user. There is no per-claim attempt
counter: a wrong guess is looked up by digest and matches no row, so it could
not be attributed to the code it was aiming at — rate limiting is the only
mechanism that can see the attempt.

The **provisioning token** is 256 bits and never displayed. Only the device
that began provisioning can exchange it for a credential, and the exchange
works exactly once. This is what keeps reading the code off someone's screen
from escalating into impersonating their device.

The resulting **device token** is 384 bits, prefixed `novad_` so secret
scanners can match it, and stored only as a SHA-256 digest. It is checked on
every WebSocket handshake, and the device's row is re-read at the same time,
so releasing or re-provisioning a device invalidates it immediately rather
than at some later expiry.

`hardware_id` is an identifier, never a credential. It is printed on the chip
and trivially spoofed, so it determines which row is provisioned and never who
owns it.

Physical possession is treated as authority to reset, as on consumer hardware:
re-provisioning revokes existing credentials and clears the owner, so a resold
or recovered NOVA stops reporting to whoever had it last.

### The device protocol

Authentication happens at the handshake, before the socket is accepted, so an
unauthenticated peer never reaches a state where it can send frames. Every
frame is validated against a discriminated union with `extra="forbid"`, and
oversized payloads are rejected before the JSON decoder sees them. A peer that
sends repeated malformed frames is disconnected with a distinct close code.

Servo ranges are enforced at the boundary and rejected rather than clamped:
silently changing what was asked hides the bug that produced it.

## Secrets

Never committed: API keys, passwords, tokens, `.env` files, private
certificates, `.p8`/`.pem`/`.key` files. `.gitignore` covers these broadly and
CI runs secret scanning.

`NOVA_JWT__SECRET_KEY` has **no default**. The API refuses to start without
it, and rejects anything shorter than 32 characters. Generate one per
environment:

```bash
openssl rand -base64 48 | tr -d '\n'
```

Rotating it invalidates every outstanding access token. Refresh tokens survive
rotation, since they are database rows rather than signed claims — users
recover on their next refresh instead of being signed out.

## Privacy

The microphone drives the defaults. **NOVA has no camera**
([ADR 008](docs/decisions/008-amoled-face-hardware.md)) — presence is a
time-of-flight distance reading, which can establish that something is 72 cm
away and nothing about who it is.

| Data | Default | Notes |
|---|---|---|
| Raw audio | **Not stored** | Opt-in only |
| Video | **N/A** | No camera exists |
| Presence events | Stored | Structured (`person_detected`, distance) — a range measurement, not an identity |
| Motion events | Stored | IMU-derived: picked up, tilted, tapped |
| Voice events | Stored | Structured metadata — not recordings |
| Conversations | Stored | User-viewable and deletable |
| Memories | Stored | User-viewable, editable, deletable |

Account deletion cascades to every owned row via `ON DELETE CASCADE`.

Memory extraction is designed to record preferences and context, not
credentials or identifiers. Users can inspect and delete anything NOVA
believes about them.

## Threat model

| Threat | Mitigation |
|---|---|
| Credential stuffing | Argon2id, rate limiting, identical failure responses |
| Account enumeration | Identical responses and equalised timing |
| Database leak | Passwords Argon2id-hashed; refresh tokens stored as digests |
| Stolen refresh token | Rotation with family-wide reuse detection |
| Stolen access token | 15-minute lifetime; database check for active status |
| JWT forgery | Pinned algorithm, required claims, issuer and audience checks |
| Token replay after logout | Server-side revocation, checked on every refresh |
| SQL injection | Parameterised queries throughout; no string-built SQL |
| Info disclosure via errors | Generic 500s; validation echoes no input |
| Log injection | Untrusted request IDs rejected unless a valid UUID |
| Privilege escalation via container | Non-root runtime user, no build tools in image |

## Planned (later phases)

- **Device authentication** (Phase 2): per-device credentials, distinct from
  user tokens, with independent revocation.
- **WebSocket authentication** (Phase 2): authenticated at handshake;
  strict schema validation on every frame.
- **Webhook verification** (Phase 9): GitHub signature validation with
  constant-time comparison.
- **Security review** (Phase 10): dependency audit, penetration testing pass,
  formal threat-model review.
