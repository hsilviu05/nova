# 001 — React Native and Expo for the mobile client

**Status:** Accepted · **Date:** 2026-09-06

## Context

NOVA needs a mobile app for chat, memory management, device control, and
analytics. It must run on iOS and Android, feel native, and be maintainable by
one developer alongside a Python backend, ESP32 firmware, and an ML pipeline.

Development speed matters disproportionately here. A companion device is
judged on how it feels, which means many iterations on interaction details.

## Decision

**React Native with Expo**, TypeScript, Expo Router, Zustand for client state,
TanStack Query for server state.

Zustand and TanStack Query cover different problems and both are needed.
TanStack Query owns anything the server is the source of truth for — caching,
revalidation, request deduplication, optimistic updates. Zustand owns state
the server never sees: the active conversation draft, UI toggles, WebSocket
connection status. Forcing either job into the other tool produces either a
hand-rolled cache or a global store full of server data.

## Alternatives considered

**Two native apps (Swift + Kotlin).** The best possible feel, and roughly
double the client work for a single developer. Rejected: the time would come
out of the firmware and ML phases, which are what make this project
distinctive.

**Flutter.** Genuinely good, with excellent rendering consistency. Rejected on
ecosystem fit: the backend and tooling are already TypeScript-adjacent, and
sharing types and idioms across the stack is worth more here than Flutter's
rendering advantages. Dart would also be a fifth language in the project.

**Native iOS only.** Halves the work and matches the primary device. Rejected:
it makes the project a demo rather than a product, and Android exclusion is
hard to justify in a portfolio piece.

**Bare React Native, no Expo.** More control over native modules. Rejected for
now: Expo's build service, OTA updates, and managed native dependencies remove
substantial toolchain overhead. Expo's prebuild path means dropping to bare is
possible later without a rewrite if a native module demands it.

## Consequences

- One codebase, two platforms, TypeScript shared with backend contracts.
- Expo Router's file-based routing keeps navigation legible as screens grow.
- Some native capability requires a config plugin or a prebuild step. BLE
  provisioning in a later phase is the most likely trigger.
- Bundle size is larger than native. Acceptable for this application class.
- Expo SDK upgrades are periodic work that must be budgeted, not ignored.
