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
| `core/protocol.*` — the versioned frames | **Done, tested both ways** |
| `core/pca9685.*` — servo driver command encoding | **Done, tested** |
| `core/rangefinder.hpp` — reading validity, median filter | **Done, tested** |
| `core/outbox.hpp` — offline telemetry queue | **Done, tested** |
| `core/connection.*` — reconnect policy | **Done, tested** |
| `core/face.*` — expressions, blinking, gaze, rasterising | **Done, tested** |
| `main/` — I²C, servos, rangefinder, NVS, WiFi, WebSocket, panel | Written, **never compiled** |
| Panel controller bring-up | Not started — needs the board |
| Provisioning UI | Not started |

**Nothing here has run on hardware.** There is no board yet. What *is* true
is that everything in `core/` compiles under `-Wall -Wextra -Wpedantic
-Werror`, its tests pass, and the protocol is checked against the server's
own schema in both directions.

`main/` is a different claim, and a weaker one. It **cannot be compiled in
this repository's CI**: it needs ESP-IDF and the Xtensa toolchain, neither of
which is installed here. It has been written and reviewed, and it has never
been through a compiler. Expect to fix build errors on first bring-up. That
gap is the whole reason the split exists and the reason `main/` is as thin as
it is — every part of the system with a decision in it was pushed across the
line into `core/`, where a test can reach it.

### What `main/` contains

| File | Job |
|---|---|
| `app_main.cpp` | Wiring only: sensors → `Observation` → engine → `Action` |
| `i2c_bus.*` | The one shared bus, with the mutex that makes two tasks safe |
| `servos.*` | Replays `core/pca9685` sequences; releases the head when idle |
| `distance.*` | VL53L0X single-shot reads, fed into `core/rangefinder` |
| `display.*` | Framebuffer in PSRAM, span blitting, dirty-region flush |
| `nvs_store.*` | WiFi credentials and the device token. Logged nowhere |
| `wifi.*` | Station mode; distinguishes a refused password from no network |
| `ws_link.*` | The WebSocket, and reading close code 4001 |

### Known gaps

- **The rangefinder runs uncalibrated.** `distance.cpp` uses the sensor's
  power-on defaults: no tuning register set, no SPAD calibration. Absolute
  accuracy is nearer ±10% than ±3%. NOVA needs "is somebody within ~70 cm"
  with 30 cm of hysteresis and an 800 ms debounce, and 10% does not reach
  those thresholds. Adding ST's driver later changes nothing above it.
- **Timestamps are uptime, not wall time.** Until an RTC sync lands,
  `recorded_at` is time since boot, marked as such rather than fabricated —
  a plausible-looking wrong timestamp would corrupt every habit the analytics
  phase infers.
- **There is no provisioning UI.** A device with no stored credentials logs
  what is missing and stops, because showing a claim code needs the panel
  driver. It names the state it is stuck in rather than pretending.
- **NVS is not encrypted** unless flash encryption is enabled in hardware,
  which is a production step, not something the firmware can do to itself. An
  attacker with physical access and a flash reader can read the device token;
  the mitigation is that it is revocable from the dashboard.
- **The panel controller is not chosen.** `Display::begin` takes an
  already-initialised `esp_lcd_panel_handle_t`, and nothing here creates one.
  An AMOLED's initialisation is a vendor-specific command list, and inventing
  one from memory is how a display stays black with no error to explain it —
  the same reason the rangefinder runs on factory defaults. Add the vendor
  component for the board in hand and pass the handle in; everything above
  that line is written and tested. Until then the face falls back to logging
  its state changes, so a bring-up session can still watch the engine work.

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

## The face

The product thesis is a desk creature, not a speaker with a screen, so the
face is where it stands or falls (ADR 008). An AMOLED renders true black — an
unlit pixel emits nothing — so eyes drawn on it float in the bezel rather than
sitting on a visible rectangle. The background is therefore never drawn: not
drawing it is both the correct look and free.

The expressive vocabulary is deliberately small. Each eye is a filled rounded
rectangle with a position, a size and a corner radius. No pupils, no slanted
lids, no brows. Cozmo and Vector do more, but they blit whole frames; NOVA
emits two rectangles, and openness, vertical offset, gaze and asymmetry
between the two eyes turn out to carry all ten behaviour states.

What makes it read as alive is in `core/`, where tests can reach it:

- **Blinking on a randomised interval.** A metronomic blink is the clearest
  tell that something is a machine — people notice without being able to say
  why. Gaps are 2.2–7 s, and there is a test that the gaps actually differ.
- **Idle gaze drift.** Eyes holding one position perfectly read as a doll.
  Both eyes drift together; independently drifting eyes read as a fault.
- **Eased transitions.** Linear interpolation starts and stops abruptly,
  which nothing alive does. Integer smoothstep, no FPU.
- **A shut eye is a line, not nothing.** Collapsing the height to zero makes
  the face vanish mid-blink, which looks like a crash.
- **A sleeping face does not blink or drift.** A sleeping face that blinks is
  not asleep, and drifting behind shut eyes burns the panel for nothing.

And the part that matters for battery: `FaceAnimator::update` returns a frame
**only when the pixels actually differ**. The panel is the largest draw on the
board, so a settled face costs no redraws rather than sixty identical ones a
second. `Display::draw` then sends only the bounding box of the old face and
the new one — a blink is about 30 KB over the wire instead of 412 KB.

The eyes cannot leave the panel, and that is a `static_assert` rather than a
runtime clamp. A clamp would be an unreachable branch no test could exercise,
and it would silently distort the face if it ever fired. Widen
`kSaccadeRangeX` past what the margins allow and the build fails naming the
constant.

## The protocol is checked against the server, both ways

`core/protocol.*` mirrors `services/api/src/nova/schemas/protocol.py`. Two
implementations of one wire format in two languages will drift; this is what
notices.

**Server to device.** The fixtures the C++ tests decode are *generated from
the Pydantic models*, not written by hand on this side. A fixture invented
here would only prove the decoder agrees with the decoder.

**Device to server.** `tools/emit_frames.cpp` prints every frame the firmware
can produce, and `scripts/check_protocol_contract.py` validates each one with
the real Pydantic models — the same validation a live connection performs.

```bash
python scripts/check_protocol_contract.py
```

Verified by breaking it three ways: misspelling a field name, sending the
version as a string, and sending an out-of-range `wifi_rssi`. Each is
rejected, and the failure names the frame and the field.

Two rules carried over from the server:

- **Unknown frames are rejected, not guessed at.** A command that is *almost*
  valid moves real servos. An out-of-range angle is refused rather than
  clamped — the server enforces the same limits, so a command outside them
  means something upstream is wrong, and quietly obeying a changed version of
  it hides the bug that produced it.
- **Every frame carries a version.** The firmware ships inside a physical
  object that may not be reflashed for months, so the backend will eventually
  be talking to an older protocol than it prefers.

## Dependencies

One, and it is not really an added one: **cJSON**, vendored under
`third_party/cJSON` (MIT, v1.7.19). ESP-IDF already bundles cJSON, so on the
device this costs nothing; the vendored copy exists so the host tests exercise
the same parser the firmware will run. Writing a JSON parser by hand to avoid
97 KB would have been the worse trade — hand-rolled parsers are a well-known
source of exactly the bugs this layer must not have.

Nothing else. No test framework, no numerics library.

## Building for the device

```bash
. $IDF_PATH/export.sh
idf.py set-target esp32s3
idf.py menuconfig      # NOVA → Backend base URL
idf.py build flash monitor
```

**ESP-IDF v5.2 or later.** The floor is the `i2c_master` driver, which
replaced the legacy `driver/i2c.h` API in that release. The board is an
ESP32-S3 with 16 MB flash and 8 MB PSRAM.

`core/` is compiled into the app as a component that references the sources
in place — the same translation units the host tests exercise are the ones
that run on the device. A device-side copy would let the two drift, and the
drift would be invisible until it was a robot behaving oddly on a desk.

Set the backend URL before flashing. It is compiled in rather than
provisioned at runtime, deliberately: a runtime-settable server URL is a way
to redirect somebody's device to an attacker. Use `wss://` — the device token
travels in the query string, and over `ws://` it travels in clear text.

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
