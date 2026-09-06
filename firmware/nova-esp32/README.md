# NOVA Firmware

ESP-IDF firmware for the Waveshare ESP32-S3-Touch-AMOLED-2.06. **Phases 2 and 6.**

Board choice and its constraints:
[ADR 008](../../docs/decisions/008-amoled-face-hardware.md). Protocol:
[ADR 004](../../docs/decisions/004-websocket-device-protocol.md).

## Module layout

Planned, and deliberately not one `main.cpp`:

```
src/
├── main/          entry point and wiring
├── network/       WiFi manager, reconnection
├── protocol/      message parsing and validation
├── display/       AMOLED driver and framebuffer
├── face/          eye animation and expression rendering
├── touch/         capacitive touch input
├── audio/         ES8311 codec: microphone capture and playback
├── servo/         head yaw and pitch, via PCA9685 over I2C
├── sensors/       VL53L0X distance, 6-axis IMU
├── telemetry/     event batching and dispatch
├── state/         behaviour state machine
└── storage/       NVS configuration, RTC
```

## Hardware notes

**Every off-board peripheral shares one I²C bus** on GPIO14/15. The board
reserves only I²C, UART, and USB pads for expansion, so servos run through a
PCA9685 rather than direct PWM.

| Device | Address | Location |
|---|---|---|
| Touch controller | — | onboard |
| 6-axis IMU | 0x6B | onboard |
| RTC | 0x51 | onboard |
| VL53L0X | 0x29 | external |
| PCA9685 | 0x40 | external |

Reserved, do not repurpose: GPIO19/20 (native USB — multiplexing breaks
flashing and debugging), GPIO43/44 (UART0 logging), GPIO0 (boot select).

`LCD_CS` and `IMU_INT1` swap between GPIO9 and GPIO46 across hardware
revisions in this board family. Detect the revision rather than hardcoding it.

## The face

The AMOLED is the primary expressive surface, so eye animation is a
first-class subsystem rather than an LED blink pattern. True black on AMOLED
means drawn eyes float in the bezel and read as a face rather than a screen.

Each state drives face, head, and audio together:
`IDLE → LISTENING → THINKING → SPEAKING`, plus `CURIOUS`, `HAPPY`,
`CONFUSED`, `ALERT`, `SLEEPING`, `OFFLINE`.

## Offline behaviour

The device must keep behaving when the backend is unreachable. On disconnect
it enters `OFFLINE` and runs the local state machine: idle eye animation,
proximity reactions, touch and motion responses. The onboard RTC keeps
time-aware behaviour working without a backend round trip.

## No camera

See ADR 008. `person_detected` is emitted by the time-of-flight sensor rather
than by vision, and the IMU adds pickup, tilt, tap, and shake as interaction
events.
