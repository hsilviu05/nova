# Security review

A pass over the running API, made before the first deployment. Each item
is a claim about the system as it is now, with what was looked at and
what was done. [SECURITY.md](../SECURITY.md) describes the design; this
is the check that the design is what runs.

**Scope.** The API service and its deployment. The firmware and the iOS
app were not reviewed here: neither has run on its hardware yet, and a
review of code that has never executed is a code review, not a security
review.

**Method.** Read the middleware stack, the auth service, every route's
dependencies, the settings layer and the compose files; then exercised the
running service in production mode behind simulated proxy headers to
confirm each claim below with a request rather than by reading. CI already
runs `pip-audit`, `gitleaks` over full history and ruff's bandit ruleset
on every push; those were clean at the time of writing and are not
repeated here.

## Findings

Severity is what an attacker could have done with it, not how hard it was
to fix.

### High

**H1. No cap on request body size.** *Fixed.* Any endpoint would read a
body of any size into memory. One client could exhaust a worker with a
single request. There is now a 1 MiB cap enforced on the endpoint's own
read of the body, so a route that never reads it is not refused for a
header alone, and a chunked upload that lies about its length is stopped
at the byte it crosses the cap. The GitHub webhook keeps its own tighter
limit.

A second bug was found while confirming the fix against the real login
route: the refusal came back as FastAPI's own 400 "error parsing the
body", because FastAPI wraps its body read in a catch‑all and the 413
raised from the read never reached its handler. The middleware now sends
the 413 envelope itself. The unit test that had passed covered only a
route that read the raw body; it now covers the request‑model path, which
is every route that matters.

**H2. Nothing prevented a production boot with development settings.**
*Fixed.* The CI secret, a CORS wildcard, `debug=true`, SQL echo, plain
HTTP — each is a plausible copy‑paste from `.env.example` into a
production `.env`, and each would have started without complaint. The
settings layer now refuses to construct in production with any of them
and names every problem in the error. The release workflow's smoke test
is this guardrail firing on the published image.

### Medium

**M1. No security response headers.** *Fixed.* Every response now
carries `Cache-Control: no-store` (every response is either personal or
an error), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, and a `Permissions-Policy` that denies
camera, microphone and geolocation. HSTS is set only when the request
actually arrived over HTTPS, as reported by the proxy; a plain‑HTTP dev
server never pins a browser to a scheme it cannot serve.

**M2. No Host header validation.** *Fixed.* A request for a host the API
does not serve is either a misrouted proxy or a cache‑poisoning attempt.
There is now an allowlist, wildcard by default for local work and refused
by the production guardrail. Verified: a request with the wrong Host is a
400 before it reaches any route.

**M3. Rate limits keyed on the proxy's address.** *Fixed in deployment.*
The limiter keys on the client address. Behind a reverse proxy that
address is the proxy's unless the forwarded headers are honoured, so
every client would have shared one bucket — a single abusive client
would lock everyone out of login. The production compose runs uvicorn
with `--proxy-headers`, trusting them from any peer, which is safe there
because the API publishes no port and the only peer that can reach it is
Caddy. Verified in production mode: eleven bad logins from eleven
forwarded addresses are eleven 401s; eleven from one address end in a
429.

**M4. Development Postgres and Redis published on every interface.**
*Fixed.* Redis has no password. Both are now bound to loopback in the
development compose file and publish nothing at all in the production
one.

### Low

**L1. `alembic upgrade head` runs on every API start.** *Accepted.* A
migration is applied by whichever replica starts first, under Alembic's
own lock. With one host and one API service this is the simplest correct
thing; with several it would want a separate migrate step.

**L2. The device token travels in the WebSocket URL's query string.**
*Accepted, documented.* Caddy's access log is off by default and the
API's own access log records the path without the query. Over TLS the
query is encrypted in transit. The firmware README already says to use
`wss://` only.

**L3. NVS on the device is unencrypted without flash encryption.**
*Out of scope here, documented in the firmware README.* The mitigation is
that the token is revocable from the dashboard.

## Confirmed good

Things that were checked and needed no change, listed so the next
reviewer does not re‑derive them.

- **Passwords**: Argon2id, opportunistic rehash on login when the cost
  parameters change. Timing on a failed login is equalised whether or not
  the email exists.
- **Access tokens**: `iss`, `aud`, `jti`, `typ` and expiry are all
  required and checked; a refresh token cannot be presented as an access
  token. Deactivation takes effect on the next request, not at expiry,
  because the user row is read on every authenticated call.
- **Refresh tokens**: opaque, hashed at rest, single‑use, rotated, with
  reuse detection that revokes the whole family.
- **The 500 handler** logs the exception and returns a fixed message and
  a request id. No exception text, no schema names, no paths reach the
  client. Validation errors return field locations and types but never
  echo the submitted value.
- **OpenAPI and the docs UI** are off in production. Verified: `/docs`
  is a 404.
- **CORS** is opt‑in with an explicit origin list, and the guardrail
  refuses a wildcard.
- **The container** runs as uid 10001 with no compiler in the runtime
  image. Verified by reading the Dockerfile; the image could not be built
  in the review environment (the registry is blocked there), so the
  running image was not inspected.
- **The webhook** verifies an HMAC over the raw bytes with a constant‑time
  compare before doing anything else, replays are deduplicated by
  delivery id, and the reaction vocabulary is a fixed table the model is
  never consulted for.
- **Secrets**: none in the repository. `gitleaks` over full history is
  clean with two reviewed fingerprints, each a test fixture shaped like a
  credential.

## Not done

Named so they are not mistaken for done.

- **No external penetration test.** This is a self‑review by the author
  of the code, with all the blind spots that implies.
- **No fuzzing of the device protocol.** Frames are schema‑validated and
  unknown ones are rejected, but no one has thrown malformed bytes at the
  WebSocket to see what a parser does under duress.
- **No review of the firmware or iOS code**, as above.
- **The production stack has not run on a public host.** It was
  validated with `docker compose config` and its application‑level
  behaviour (guardrail, headers, Host check, forwarded‑address limiting,
  body cap) was exercised in production mode on the development machine.
  The TLS issuance and the Caddy configuration have not been seen working
  end to end.
