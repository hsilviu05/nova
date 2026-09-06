# NOVA firmware

ESP32-S3 firmware for the Waveshare ESP32-S3-Touch-AMOLED-2.06.

## The split that matters

```
  core/    pure C++, no ESP-IDF     ── compiles and is TESTED on a host
  main/    ESP-IDF: WiFi, sockets,  ── needs the Xtensa toolchain
           drivers, display
```

Everything that *decides* lives in `core/`. Everything that *touches
hardware* lives in `main/`. That is not tidiness — it is what makes the
behaviour engine testable at all, and it is the same boundary the
architecture depends on elsewhere:

> **The language model never reaches GPIO.** It emits a high-level intent
> (`Attend`, `Express`, `Speak`); the behaviour engine decides what that
> means for a servo and a face. Nothing upstream of the engine can address a
> pin, and nothing downstream of it makes a decision.

An intent is a *request*, not an order. The engine declines some: a `Speak`
while the device is being picked up is dropped, because talking to someone's
hand is worse than staying quiet. `BehaviourEngine::apply` is where that
judgement lives, and there is a test for it.

## Status

| Part | State |
|---|---|
| `core/geometry.hpp` — servo mapping, travel limits | **Done, tested** |
| `core/behaviour.*` — state machine, presence, intents | **Done, tested** |
| Protocol codec — the versioned frames | Next |
| ESP-IDF layer — WiFi, WebSocket, NVS | Not started |
| Drivers — PCA9685, VL53L0X | Not started |
| Face rendering | Not started |

**Nothing here has run on hardware.** There is no board yet. What *is* true
is that the core compiles under `-Wall -Wextra -Wpedantic -Werror` and its
tests pass — 309 checks across two suites.

The ESP-IDF layer, when it exists, **cannot be compiled in this repository's
CI**: it needs the Xtensa toolchain, which is not installed here. That is
precisely why as much logic as possible lives in `core/`.

## Running the tests

No dependencies beyond a C++20 compiler and CMake.

```bash
cd firmware
cmake -S test -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

Or without CMake:

```bash
g++ -std=c++20 -Icore/include -Itest \
    -o /tmp/t test/test_behaviour.cpp core/src/behaviour.cpp && /tmp/t
```

There is no test framework. `test/check.hpp` is forty lines and gives named
cases, file and line on failure, and a non-zero exit status — which is what
CI needs. Pulling in GoogleTest would make the test framework the largest
dependency in a project that otherwise has none.

## What the tests are actually about

Read `test_behaviour.cpp` as a list of ways a desk companion can be
annoying:

- greeting you twice because a sensor reading wobbled by a centimetre
- reacting to a hand passing the sensor as though somebody arrived
- redrawing an AMOLED every tick while you sit still, which is the largest
  power draw on the board
- cutting a reply off mid-sentence because presence flickered
- talking calmly while being carried across a room

Each is a case. Each was verified by breaking the guard and watching the
right test fail — presence hysteresis, the debounce, and the picked-up
priority were all checked that way.

## Building for the device

Once the ESP-IDF layer exists:

```bash
. $IDF_PATH/export.sh
idf.py set-target esp32s3
idf.py build flash monitor
```

You need ESP-IDF v5.x. The board is ESP32-S3 with 16 MB flash and 8 MB PSRAM.

## Pins

The pin map, the shared I²C bus, and the pins that must not be repurposed are
in [`hardware/BOM.md`](../hardware/BOM.md). Two things worth repeating:

- **The board has no free PWM GPIO.** Servos go through a PCA9685 on I²C.
  This is not a preference; see ADR 008.
- **`LCD_CS` and `IMU_INT1` swap between GPIO9 and GPIO46 across board
  revisions.** Identify which revision you have before wiring anything.

## Design notes

**The engine takes a clock, it does not read one.** `Observation::now_ms` is
passed in. That makes a five-minute sleep timeout testable in microseconds,
and it means a time sync mid-run cannot make the device think an hour
passed.

**Presence uses hysteresis and a debounce.** Near is 70 cm, far is 100 cm,
and a reading must hold for 800 ms. Someone sitting at exactly the threshold
would otherwise make NOVA greet and forget them repeatedly.

**A `HeadPose` cannot be constructed out of range.** `HeadPose::clamped` is
the only constructor path, so an angle the mechanism cannot reach is not a
representable value. A servo grinding against its stop does not throw an
exception; it strips its gears overnight.

**Angles clamp rather than wrap.** A request for 200° stops at the limit.
Wrapping would send the head to the far end of travel at speed.
