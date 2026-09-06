# 004 — WebSocket, not MQTT, for the device protocol

**Status:** Accepted · **Date:** 2026-09-06

## Context

The ESP32-S3 needs bidirectional communication with the backend: telemetry and
events upward, behaviour commands downward. Latency matters — a companion that
reacts a second after you walk in feels broken.

The realistic topology is a handful of devices per user, each talking to one
backend. Not a fleet, not a mesh, no device-to-device traffic.

## Decision

**WebSocket**, carrying versioned JSON, authenticated at handshake, with
strict schema validation on every frame.

```json
{
  "type": "robot.command",
  "version": 1,
  "command": "head.move",
  "payload": {"yaw": 20, "pitch": -5, "duration_ms": 500}
}
```

A `version` field from day one. The firmware ships in a physical object that
may not be reflashed for months, so the backend will eventually be talking to
an older protocol than it prefers. Versioning after the fact is far more
expensive than carrying an integer.

Malformed messages are rejected, never best-effort interpreted. A command that
is almost valid moves real servos.

## Alternatives considered

**MQTT.** The default choice for IoT, and genuinely better for fan-out: many
publishers, many subscribers, topic routing, retained messages, QoS tiers,
last-will notifications. Rejected because NOVA has none of those problems. It
would add a broker to deploy, secure, monitor, and back up, plus a second
authentication system separate from the API's — in exchange for a topology
NOVA does not have. The one feature genuinely missed is last-will for
disconnect detection, which a heartbeat timeout already provides.

This is worth revisiting if NOVA ever grows multi-device coordination or
third-party integrations subscribing to device events.

**HTTP long-polling.** Universally supported and simple. Rejected: latency and
overhead are poor for a device that emits frequent small telemetry messages,
and the reconnect churn is worse on constrained hardware.

**Raw TCP with a custom binary protocol.** Most efficient on the wire.
Rejected: it means hand-rolling framing, TLS, reconnection, and keepalive, all
of which WebSocket already solves and all of which are easy to get subtly
wrong. JSON's overhead is irrelevant at NOVA's message rates.

**gRPC streaming.** Strong typing and code generation. Rejected: HTTP/2 plus
protobuf on ESP-IDF is a heavier dependency than the problem justifies, and
tooling support on that platform is thin.

## Consequences

- One connection, one authentication path, one TLS configuration, shared with
  the API the mobile app already uses.
- No broker to operate. Fewer moving parts to secure and monitor.
- Connection state lives in the API process. Horizontal scaling therefore
  needs sticky routing or a shared registry — Redis is already present for
  that, and it is a Phase 10 concern.
- Disconnect detection is a heartbeat timeout rather than a broker's
  last-will. Slightly slower to notice, and adequate.
- JSON is larger than a binary encoding. Irrelevant at these rates, and worth
  it for debuggability: a protocol you can read in a log is a protocol you can
  fix.
