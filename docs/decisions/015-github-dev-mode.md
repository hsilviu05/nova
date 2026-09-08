# 015 — GitHub dev mode: a public webhook, and what it may and may not do

**Status:** Accepted · **Date:** 2026-09-07

## Context

Phase 9 is one line in the roadmap: "GitHub dev mode: webhooks, CI
reactions." This ADR records what that was taken to mean, and the decisions
that came out of building the API's first — and only — unauthenticated
public endpoint.

The reading: the owner points a GitHub webhook at NOVA. When CI finishes or
a pull request merges, the creature on the desk reacts — a face and a short
spoken line — through the same command path the phone uses. It is a
creature that notices your build, not a notification channel.

## Decisions

### The reaction is a table, not a model

A green build is `happy` and "Build's green." A red one is `confused` and
"Build failed." A merge is `happy` and "Merged." That is the whole
vocabulary, fixed in `github_webhooks.py`, and the language model is not
consulted. The reasons are the same as for GPIO: it is deterministic, it is
testable, and a commit message cannot prompt-inject it. A red build is
`confused` rather than `alert` because a failed test is not an intruder.

### Verification is over the raw bytes, before anything else parses them

GitHub signs the body it sent with HMAC-SHA256 under a shared secret. The
endpoint reads the raw body, verifies the `X-Hub-Signature-256` header with
a constant-time comparison, and only then decodes JSON. Parsing first would
mean running a decoder on attacker-controlled input in order to decide
whether it came from an attacker.

The comparison is constant-time because this is the one place in the API
where a caller can make unlimited guesses without a credential.

### Refusals are ordered by cost, and everything after the signature is a 200

Unknown integration → 404, before the body is read. Body over 512 KiB →
413, from `Content-Length` where declared, and again after reading. Bad
signature → 401, with no detail about which part was wrong. After that:
duplicate delivery, disabled integration, unwatched repository, and events
NOVA does not react to are all **200 with `status: ignored`**. GitHub
retries any non-2xx response with backoff for days, and "not interested" is
not something to be retried.

### The secret is stored as it is, shown once, and rotated rather than read

HMAC needs the shared key, not a hash of it, so the secret is stored in
plain form. It is generated server-side (32 random bytes), returned in the
creation response and never again; rotation issues a new one and invalidates
the old one immediately, with no grace period — a window in which both
verify is indistinguishable from a leak that has not been noticed. This is
the same trade the device token makes in the firmware's NVS, with the same
mitigation: one call revokes it.

### Replays are deduplicated in Redis, failing open

`X-GitHub-Delivery` is remembered for 24 hours with `SET NX EX`. A
redelivery is acknowledged and ignored. If Redis is unavailable the check
passes, the same policy as the rate limiter: the worst a replay can do is
repeat a facial expression, which is not worth refusing every delivery until
Redis is back.

### Offline devices are skipped, not queued

The reaction goes to every device the owner has connected at that moment.
A device that reconnects an hour later does not receive "Build's green."
That is the same rule as phone commands: a stale reaction is noise.

### A delivery does not write telemetry

The ML dataset contract (ADR 013) has two sources: what the device observed
and what the owner said to it. A build result is neither — it is something
that happened *to* the owner, not something NOVA saw. Folding it into
`device_telemetry` would change what the label means. If GitHub events ever
become a feature, that is a third source and its own decision.

## Alternatives considered

**GitHub App instead of a webhook.** Richer (it can read checks, post
comments) and heavier (OAuth, installation tokens, JWT signing, a public
app listing). NOVA reacts to events; it never needs to call GitHub back. A
webhook is the whole requirement.

**Letting the model phrase the reaction.** Livelier, and prompt-injectable
by anyone who can name a branch. Rejected for the same reason the model
does not reach GPIO.

**Reacting to `check_suite` as well as `workflow_run`.** Both fire for the
same CI run; reacting to both makes the face flap twice. `workflow_run` is
the one that carries a conclusion for the whole workflow.

## Consequences

- One public URL exists. Its security properties are the ones above, and
  each has a test.
- The webhook URL needs the API's public name. `NOVA_PUBLIC_BASE_URL` is
  new; unset, the integration returns a path and the settings screen has to
  say so.
- `scripts/simulate_github_ci.py` signs and delivers a payload exactly as
  GitHub would, so the reaction can be watched on the simulated device
  without a public URL at all.
