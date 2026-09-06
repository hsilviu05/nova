# NOVA — printed case

Parametric OpenSCAD source for the enclosure. Four parts: a base, a yoke, a
head, and a fit test.

## ⚠️ Read this before printing anything large

**The board dimensions are derived, not measured.** Waveshare publishes no
mechanical drawing for the ESP32-S3-Touch-AMOLED-2.06, and their
documentation site was unreachable from the environment these files were
written in. The numbers in `params.scad` come from geometry:

> The display is 2.06" diagonal at 410×502 px. That gives an active area of
> 33.1 × 40.5 mm. The board outline is that plus an estimated bezel.

That is good enough to print a fit test and no better.

**Print `test_fit.scad` first.** It is 3.6 g and about twelve minutes. When
the board arrives, put a caliper on it, set four numbers in `params.scad`,
re-run the fit test, and only then print the head.

## Parts

| File | Size | ~Mass | What it is |
|---|---|---|---|
| `test_fit.scad` | 68 × 42 × 10 mm | ~4 g | Board corner, servo pocket, three screw pilot sizes |
| `base.scad` | 88 × 88 × 52 mm | ~13 g | Yaw servo, PCA9685, ToF window, cable exit, vents |
| `yoke.scad` | 29 × 53 × 30 mm | ~2 g | Sits on the yaw horn, carries the pitch servo |
| `head.scad` | 47 × 52 × 10 mm | ~2 g | Holds the display board, pivots in the yoke |

Masses are at 25% infill in PLA. The whole set is about 20 g — roughly 2 RON
of filament, so iterate freely.

Every part renders as a closed manifold solid; that is checked on each build,
but it is not a check that the thing fits together in the real world.

## Building the STLs

```bash
cd hardware/case
make            # renders every part to stl/
make test_fit   # just the fit test
make check      # verifies each part is a closed manifold
```

Or directly:

```bash
openscad -o head.stl head.scad
```

## Print settings

Tuned for a Bambu Lab printer with a 0.4 mm nozzle.

| Setting | Value | Why |
|---|---|---|
| Layer height | 0.2 mm | |
| Walls | 3+ | `wall` is 2.4 mm, which is 6 perimeters; fewer leaves the servo pockets flexible |
| Infill | 20–25% | The base carries the servo's reaction torque |
| Supports | **None** | Every part is modelled to avoid them |
| Material | PLA | Stiff and dimensionally stable. PETG creeps under a servo's steady load |

**Head orientation:** print it face down. The screen bezel becomes the first
layer and comes out sharp, and the board pocket becomes a simple upward
cavity needing no support.

## Assembly order

1. Print `test_fit`. Adjust `fit_clearance` in `params.scad` until the board
   corner slides in with light friction and no rattle.
2. Print `base`. Drop the yaw servo in from the top; it should sit flush.
   Screw through the tabs with M2 self-tappers.
3. Print `yoke`. Fit the pitch servo into its arm. Screw the yoke onto the
   yaw servo's horn using **both** holes — one screw lets the head rotate on
   the horn under load.
4. Print `head`. Fit the board, then bring the axle stub into the yoke's
   bearing hole and the horn into its pocket.
5. Route the board's cable down through the head's bottom channel, through
   the yoke, and out the base's rear slot.

**Centre the servos before assembling.** Drive both to 90° with the firmware
before you attach any horn, or you will assemble the head at the end of its
travel and discover it can only turn one way.

## Changing dimensions

Everything lives in `params.scad`. The four that matter most:

```scad
board_w        = 36.0;   // MEASURE
board_h        = 46.0;   // MEASURE
board_t        = 6.5;    // MEASURE, including the tallest component
fit_clearance  = 0.35;   // raise if tight, lower if it rattles
```

`fit_clearance` is the one you will actually tune. 0.35 mm suits a
well-calibrated Bambu at 0.2 mm layers. A printer running hot or
over-extruding wants more.

## Known limitations

- **Board dimensions unverified.** The whole point of `test_fit.scad`.
- **Servo dimensions are the standard SG90 figures.** Clones vary by a few
  tenths. If the pocket is tight, the servo is the likely culprit, not the
  model.
- **The PCA9685 standoffs assume the common 62.5 × 25.4 mm breakout.** Other
  layouts exist; measure yours.
- **No battery compartment.** V1 runs wired, per the BOM.
- **The pitch axis has no hard stop.** Travel is limited in firmware. A
  mechanical stop should come once the range of motion is settled — doing it
  now would bake in a guess.
- **Nothing has been printed.** These are rendered and dimensionally checked,
  not built. Expect the first physical fit to need adjustment.
