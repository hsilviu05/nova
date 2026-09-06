# NOVA Hardware

**Phase 6.**

- [`BOM.md`](BOM.md) — bill of materials, I²C bus map, reserved pins
- `cad/` — printable body, head, and mounts for a Bambu Lab printer
- `electronics/` — wiring diagrams and pin assignments
- `assembly/` — build instructions

Target: ~12–15 cm tall, original design, not humanoid. Roughly €100–118 in
parts.

The face is a Waveshare ESP32-S3-Touch-AMOLED-2.06: a 410×502 AMOLED that
carries the microphone, speaker, audio codec, IMU, and RTC as well as the
display. NOVA has no camera — the reasoning, and the constraints that follow
from the board reserving only I²C, UART, and USB pads, are in
[ADR 008](../docs/decisions/008-amoled-face-hardware.md).

V1 does not walk. Legged locomotion consumes most of the mechanical budget and
delivers the least; an expressive face, voice, and head movement come first.

## Build order

1. Bring the board up on USB and get the display driving.
2. Add the I²C peripherals on a breadboard — VL53L0X first, then PCA9685.
3. Add servos on their own 5 V rail, with the capacitor fitted.
4. Only then print a body and commit to soldered wiring.

Printing a shell before the electronics are proven means printing it twice.
