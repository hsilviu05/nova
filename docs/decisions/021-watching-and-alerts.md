# 021 — Watching projects: alerts on change of state, delivered by polling

**Status:** Accepted · **Date:** 2026-09-17

## Context

"Tell me when the deploy fails" was on the list from the start and always
described as two things: a scheduler and a way to reach the phone. The
README called the second one "a project of its own", and that turned out
to be the accurate part. This records what was built and what was not.

## Decisions

### The watcher raises alerts on transitions, not on failures.

Every configured project is probed each interval through the same
`project_health` tool the dashboard and chat use, so the three can never
disagree about what "up" means. A project becomes an alert when it has
failed `failures_before_alert` consecutive checks (two by default: one
dropped probe on home Wi-Fi is a blip), and again when it recovers. A
project that stays down is one alert, not one per interval. State
survives a restart by reading the latest alert per project, so last
night's outage is not announced again this morning.

It is off by default. A watcher makes requests nobody asked for, and the
one thing NOVA has been careful about throughout is not acting unprompted.

### Delivery is the phone polling, and a local notification.

The dashboard already polls status every fifteen seconds while it is on
screen. When the status says there are unacknowledged alerts, the app
fetches them and raises a local notification for each one it has not shown
before, as a banner even in the foreground. Alerts that were already on
the server when the app opened are shown on the card and not announced:
a phone that greets you with six banners for an outage you slept through
teaches you to swipe banners away unread.

For a phone on a stand with Auto-Lock off, this is the whole feature. For
a phone in a pocket it is not, and the README says so.

### APNs is a seam, not a feature.

Reaching a closed app is Apple Push Notification service: an `.p8` key
from a developer account, the `aps-environment` entitlement, a device
token registered by the app, and a signing team before any of it builds.
None of that can live in this repository, and code that cannot be run
against Apple's endpoint cannot be claimed to work. The server's notifier
is a protocol with one method and a logging implementation; an APNs
implementation is a new class behind it, with the token registration
route it would need, when there is an account to test it against.

## Alternatives considered

- **Alert on every failed check.** Trains everyone to ignore alerts within
  a week.
- **A WebSocket from the server to the phone.** The device socket the
  robot used was removed in ADR 019 for good reasons; reviving it for
  alerts would put a long-lived connection back into an app whose whole
  design is "poll while on screen, do nothing otherwise".
- **Background App Refresh.** Opportunistic and unscheduled on iOS; it
  would make delivery look reliable in demos and be unreliable in life.

## Consequences

- One task per process, cancelled cleanly on shutdown, off unless
  configured.
- The alerts table is server-wide, like the projects it describes.
- The dashboard has an alerts card with clear-one and clear-all, and a
  "watching" card when there is nothing to report, so the absence of
  alerts is visibly the watcher's finding rather than its absence.
