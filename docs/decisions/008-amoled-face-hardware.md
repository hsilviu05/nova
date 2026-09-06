# 008 — An AMOLED face instead of a camera

**Status:** Accepted · **Date:** 2026-09-06

## Context

The original hardware plan centred on the Seeed XIAO ESP32-S3 Sense, chosen
because one board carried both a camera and a microphone. Expression came from
two status LEDs, and presence detection came from the camera plus a VL53L0X
time-of-flight sensor.

Surveying available boards exposed a constraint that plan had assumed away:
in this class of hardware, **a camera and a display are mutually exclusive**.
The camera boards drive a DVP parallel interface that consumes most of the
usable GPIO; the display boards spend the same pins on a QSPI panel. No board
in the budget carries both.

So the real decision is not "should NOVA have a screen" but "which of sight or
a face does NOVA give up".

## Decision

**Waveshare ESP32-S3-Touch-AMOLED-2.06.** A 2.06-inch 410×502 capacitive touch
AMOLED, ESP32-S3R8 with 8 MB PSRAM, a 6-axis IMU, an RTC, an ES8311 audio
codec with onboard speaker and microphone array, a TF slot, and USB-C with
LiPo charging.

NOVA gets an animated face and loses the camera.

### Why the face wins

The product thesis is "a living desk creature rather than a smart speaker."
Emotional presence is the feature. An AMOLED renders true black, so eyes drawn
on it float in the bezel and read as a face rather than as a screen — which is
most of what makes Cozmo and Vector feel alive. Two LEDs cannot approach that.

The camera was also the weakest part of the original plan. On-device inference
on an ESP32-S3 is severely limited, so anything real meant streaming JPEG
frames over WiFi to the backend at a few frames per second — slow,
power-hungry, and productive of fairly crude events.

### Why this does not damage the data-science story

This was the deciding analysis. The interaction-prediction feature set is
hour, day of week, time since last interaction, interactions in the last hour
and day, average session duration, **distance**, and device state. Every one of
those survives: presence, approach, and dwell time come from the VL53L0X, not
from vision. `person_detected` becomes a time-of-flight event rather than a
vision event, with distance rather than a classifier confidence.

The behavioural ML pipeline — the actual differentiator — is unaffected. What
is lost is the computer-vision claim, and that is recoverable later either by
adding a second sensor board or, better, by doing vision on the iOS client
with the Vision framework on far more capable hardware.

### The constraint this imposes

Waveshare reserves **only I²C, UART, and USB pads** for expansion: GPIO14/15
carry the shared I²C bus, GPIO19/20 are native USB, GPIO43/44 are UART0, and
GPIO0 is the boot button. There is **no free PWM-capable GPIO for servos**, and
the pads are solder pads rather than a header.

Servos therefore go through a **PCA9685 16-channel PWM driver on the I²C bus**.
Every off-board peripheral hangs off that one bus:

| Device | Address | Location |
|---|---|---|
| Touch controller | — | onboard |
| 6-axis IMU | 0x6B | onboard |
| RTC | 0x51 | onboard |
| VL53L0X time-of-flight | 0x29 | external |
| PCA9685 servo driver | 0x40 | external |

No address collisions. Driving servos over I²C adds a few milliseconds of
latency versus direct PWM, which is irrelevant for head movement.

## Alternatives considered

**Keep the XIAO ESP32-S3 Sense, add a small SPI display.** Preserves camera,
microphone, and a face. Rejected on pin budget: an SPI panel, two servos, I²C
for the ToF, and I²S for audio need eleven pins against the eleven the Sense
board exposes — zero margin, before accounting for pins its expansion board
already consumes internally. A design with no slack is one that fails during
assembly rather than during planning.

**Two boards: XIAO Sense for senses, AMOLED for the face.** Keeps every
capability. Rejected as disproportionate for V1: two microcontrollers means an
inter-processor protocol, two firmware images, two update paths, and roughly
€65 of controllers alone. It is a reasonable V2 if computer vision becomes
worth that cost.

**ESP32-S3-Touch-AMOLED-1.64** (the board originally shortlisted, ~€35 at the
cheaper Romanian seller). Rejected: no microphone at all. A companion that
cannot hear has no LISTENING state and no voice pipeline, which is a larger
loss than the camera. Adding an INMP441 would fix hearing but still leaves no
speaker, no codec, and the same pin problem.

**ESP32-S3-Touch-AMOLED-1.75**, 466×466 round with a dual microphone array. A
genuinely close second, and the round panel reads as a creature eye. Rejected
narrowly: the 2.06 carries a speaker and the ES8311 codec as well as the
microphones, closing the audio loop on the board itself.

**Screen-only, no servos.** Would sidestep the PCA9685 entirely. Rejected: it
removes the physical dimension that distinguishes NOVA from an app.

## Consequences

- **The audio pipeline is solved on-board.** The ES8311 codec has both ADC and
  DAC, so the microphone, speaker, and amplifier disappear from the bill of
  materials. It is a better part than the MAX98357A it replaces, which is a
  DAC only.
- **The peripheral count drops.** Amplifier, speaker, external microphone,
  status LEDs, and tactile buttons are all removed — the touchscreen covers
  physical input and the display covers expression. Net cost falls by roughly
  €15 despite the more expensive controller.
- **Servos require the PCA9685.** Non-negotiable, not an optimisation.
- **Two capabilities appear that were never planned.** The IMU makes being
  picked up, tilted, tapped, or shaken into detectable events — a new
  interaction modality and new ML features for a desk creature. The RTC keeps
  time-aware behaviour working while offline.
- **Firmware gains a display and animation subsystem** and loses its camera
  module. Eye animation becomes a first-class concern rather than an LED
  pattern.
- **`VisionEvent` leaves the Phase 6 scope.** The database entity stays defined
  but unused until a camera exists; `person_detected` is emitted by the
  time-of-flight sensor.
- **Assembly is more delicate.** Soldering 30AWG wire to reserved pads is
  finer work than plugging into a header, and it is the highest-risk step in
  the build.
- **Battery support arrives early but stays unused.** The board has an MX1.25
  LiPo interface; V1 still runs wired, because adding power management before
  the firmware is stable means debugging two systems at once.
- **The backend is unchanged.** Telemetry is already a versioned, validated
  message schema, so swapping a vision event for a time-of-flight event is a
  payload change, not an architectural one.
