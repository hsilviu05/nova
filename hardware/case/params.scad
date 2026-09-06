// NOVA — shared parameters for every printed part.
//
// Change numbers here, not in the part files. Every part includes this, so a
// board that measures differently than expected is one edit, not four.
//
// ===========================================================================
// MEASURE THESE WHEN THE BOARD ARRIVES
// ===========================================================================
// The Waveshare product page does not publish a mechanical drawing, and the
// documentation site was unreachable while this was written. The four numbers
// below are DERIVED, not measured:
//
//   The display is 2.06" diagonal at 410x502 px, which gives an active area
//   of 33.1 x 40.5 mm. The board outline is that plus an estimated bezel.
//
// They are close enough to print a test frame and no closer. Put a caliper on
// the real board before printing anything you intend to keep, and print
// test_fit.scad first -- it is 4 g and five minutes.
// ===========================================================================

board_w        = 36.0;   // MEASURE: board outline width
board_h        = 46.0;   // MEASURE: board outline height
board_t        = 6.5;    // MEASURE: thickness including the tallest component
screen_w       = 33.1;   // derived from 410 px at 2.06" diagonal
screen_h       = 40.5;   // derived from 502 px at 2.06" diagonal

// How much bigger than the board the pocket is cut. 0.35 mm suits a Bambu
// printer at 0.2 mm layers; raise it if parts bind, lower it if the board
// rattles.
fit_clearance  = 0.35;

// ---------------------------------------------------------------------------
// Print settings this model assumes
// ---------------------------------------------------------------------------
wall           = 2.4;    // 6 perimeters at 0.4 mm nozzle
floor_t        = 2.0;
nozzle         = 0.4;

// ---------------------------------------------------------------------------
// SG90 micro servo — standard dimensions.
// ---------------------------------------------------------------------------
// These are the common SG90 figures. Clones vary by a few tenths; check yours
// if the pocket is tight.
servo_body_l   = 23.0;
servo_body_w   = 12.4;
servo_body_h   = 22.8;
servo_tab_l    = 32.4;   // across the mounting tabs
servo_tab_t    = 2.6;
servo_tab_hole = 2.1;    // for an M2 self-tapping screw
servo_tab_span = 27.9;   // hole centre to hole centre
// Output shaft centre, measured from the body edge nearest it.
servo_shaft_offset = 5.9;
servo_shaft_d  = 4.8;    // spline outer diameter, plus clearance

// ---------------------------------------------------------------------------
// VL53L0X time-of-flight module (GY-530 breakout)
// ---------------------------------------------------------------------------
tof_pcb_w      = 11.0;
tof_pcb_h      = 21.0;
tof_pcb_t      = 1.6;
tof_window_d   = 5.0;    // opening in front of the sensor
// Distance from the top edge of the breakout down to the sensor centre.
tof_sensor_y   = 4.5;

// ---------------------------------------------------------------------------
// Base
// ---------------------------------------------------------------------------
base_d         = 88.0;   // outer diameter
base_h         = 52.0;   // floor to top face
base_taper     = 6.0;    // how much narrower the top is; keeps it from
                         // looking like a tin can
cable_slot_w   = 9.0;
cable_slot_h   = 5.0;

// ---------------------------------------------------------------------------
// Head and pitch axis
// ---------------------------------------------------------------------------
axle_d         = 4.0;    // pitch pivot pin
axle_clearance = 0.4;
head_wall      = 2.4;

// ---------------------------------------------------------------------------
// Fasteners — M2 self-tapping into plastic.
// ---------------------------------------------------------------------------
// Sized for screwing straight into PLA, which is fine for a prototype. If you
// switch to heat-set inserts later, open these to the insert's spec.
m2_pilot_d     = 1.7;
m2_head_d      = 4.0;
m2_head_t      = 1.8;

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
$fn = $preview ? 48 : 120;

// Nudge used to break coplanar faces on differences. Coplanar faces are the
// usual cause of a mesh that slicers report as non-manifold.
eps = 0.01;

// A rounded slot, used for vents and the cable exit.
module rounded_slot(length, width, height) {
    hull() {
        for (x = [-1, 1])
            translate([x * (length - width) / 2, 0, 0])
                cylinder(d = width, h = height, center = true);
    }
}

// A screw boss with a pilot hole.
module screw_boss(height, outer_d = 6.0) {
    difference() {
        cylinder(d = outer_d, h = height);
        translate([0, 0, -eps])
            cylinder(d = m2_pilot_d, h = height + 2 * eps);
    }
}
