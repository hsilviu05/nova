# Bill of materials

Everything needed to build one NOVA. Prices are EU estimates from September
2026 and will drift — treat them as budgeting figures, not quotes.

Hardware reasoning is in
[ADR 008](../docs/decisions/008-amoled-face-hardware.md).

## Core

| # | Part | Qty | Est. | Notes |
|---|---|---|---|---|
| 1 | **Waveshare ESP32-S3-Touch-AMOLED-2.06** | 1 | €45–55 | Get the **without-battery** SKU. Both exist; V1 runs wired. |
| 2 | **USB-C cable, data-capable** | 1 | €5 | Charge-only cables cost everyone half an hour once. |

The board carries the display, touch, microphone array, speaker, ES8311 audio
codec, 6-axis IMU, RTC, and LiPo charging. There is no separate audio, LED, or
button purchase.

## Peripherals

| # | Part | Qty | Est. | Notes |
|---|---|---|---|---|
| 3 | **PCA9685 16-ch PWM driver** | 1 | €5 | **Mandatory for servos.** The board exposes no free PWM GPIO — see ADR 008. |
| 4 | **SG90 micro servo** | 3 | €9–12 | Head yaw and pitch need two. The third is because SG90 gears strip during mechanical iteration. |
| 5 | **VL53L0X ToF module** (GY-530) | 1 | €5–10 | Presence, approach, dwell. This is what emits `person_detected`. |

## Power

Servos must not draw from the board's regulator. Stall current is roughly
700 mA each; browning out the ESP32 mid-motion is the most common failure in
builds like this.

| # | Part | Qty | Est. | Notes |
|---|---|---|---|---|
| 6 | **5 V 2 A USB PSU + screw-terminal breakout** | 1 | €8 | Separate servo rail. Tie grounds to the board. |
| 7 | **470–1000 µF electrolytic capacitor** | 1 | €1 | Across the servo supply, close to the servos. Absorbs inrush. |

## Assembly

| # | Part | Qty | Est. | Notes |
|---|---|---|---|---|
| 8 | **30 AWG silicone wire** | 1 | €5 | The expansion pads are solder pads, not a header. Thin, flexible wire matters here. |
| 9 | Dupont jumpers (M-F, F-F) | 1 set | €5 | For the PCA9685 and ToF while breadboarding. |
| 10 | 400-point breadboard | 1 | €4 | Prototype before committing to solder. |
| 11 | M2 screws + heat-set inserts | 1 set | €8 | Heat-set inserts into PLA hold far better than screwing into plastic. |

## Total

| | |
|---|---|
| Core | €50–60 |
| Peripherals | €19–27 |
| Power | €9 |
| Assembly | €22 |
| **Total** | **€100–118** |

Above the original €90–100 target, driven by the controller. The alternative —
a cheaper board plus an external microphone, amplifier, speaker, LEDs, and
buttons — costs less in parts and considerably more in wiring, integration,
and risk.

**Not on this list:** battery, camera. The battery comes after the wired build
is stable; the camera is a V2 decision documented in ADR 008.

## Buying notes

- **Check more than one Romanian seller.** The 1.64" variant was listed at
  161,75 RON on Skroutz against 350,61 RON on eMAG — 2.31× for the same part.
  Verify this SKU across both before ordering.
- **Do you own a soldering iron?** If not, add €30–50. Items 3, 5, and 8 all
  need one regardless of which board you choose.

## I²C bus map

Every off-board peripheral shares the bus on GPIO14/15.

| Device | Address | Location |
|---|---|---|
| Touch controller | — | onboard |
| 6-axis IMU | 0x6B | onboard |
| RTC | 0x51 | onboard |
| VL53L0X | 0x29 | external |
| PCA9685 | 0x40 | external |

No collisions. Confirm against the board revision's pinout before soldering:
`LCD_CS` and `IMU_INT1` swap between GPIO9 and GPIO46 across hardware
revisions on this board family, so identify which revision arrived.

## Reserved pins

Do not repurpose these:

| Pin | Function |
|---|---|
| GPIO14 / GPIO15 | Shared I²C bus |
| GPIO19 / GPIO20 | Native USB — multiplexing breaks flashing and debugging |
| GPIO43 / GPIO44 | UART0, serial logging |
| GPIO0 | Boot select, wired to the BOOT button |

UART0 is technically free once firmware is stable. Taking it costs serial
logging during bring-up, which is the wrong trade while writing the firmware.
