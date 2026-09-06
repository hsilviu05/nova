// NOVA — fit test.
//
// PRINT THIS FIRST. It is about 4 g and twelve minutes.
//
// The board dimensions in params.scad are derived from the display diagonal,
// not measured from hardware -- Waveshare publishes no mechanical drawing.
// This part exists so you find that out on a coaster-sized print instead of
// on a finished head.
//
// It carries, side by side:
//   * one corner of the board pocket, at the real clearance
//   * the servo body pocket
//   * the servo mounting-tab screw spacing
//   * three test holes for M2 self-tapping screws
//
// If the board corner is tight, raise fit_clearance. If the board rattles,
// lower it. Then re-print this, not the head.
//
//   openscad -o test_fit.stl test_fit.scad

include <params.scad>

plate_t = 3.0;
gap     = 6.0;

module board_corner_gauge() {
    // An L of the pocket's two walls, so a real board corner can be dropped
    // in and felt. A full pocket would need the whole board; a corner tells
    // you the same thing about clearance.
    corner = 22;
    difference() {
        cube([corner, corner, plate_t + board_t]);

        translate([head_wall, head_wall, plate_t])
            cube([
                corner,
                corner,
                board_t + fit_clearance + eps,
            ]);
    }
}

module servo_gauge() {
    // Body pocket plus the tab screw holes, at the spacing base.scad uses.
    // If the servo does not drop in here, it will not drop into the base.
    width  = servo_tab_l + 8;
    depth  = servo_body_w + 8;

    difference() {
        cube([width, depth, plate_t + 6]);

        // Body.
        translate([(width - servo_body_l) / 2, (depth - servo_body_w) / 2, plate_t])
            cube([servo_body_l, servo_body_w, 6 + eps]);

        // Tab screws.
        for (x = [-1, 1])
            translate([width / 2 + x * servo_tab_span / 2, depth / 2, -eps])
                cylinder(d = m2_pilot_d, h = plate_t + 6 + 2 * eps);
    }
}

module screw_gauge() {
    // Three pilot diameters. M2 self-tappers into PLA are fussy: too tight
    // splits the boss, too loose strips on the second assembly. Drive a screw
    // into each and keep the number that bites without cracking.
    labels = [1.5, 1.7, 1.9];
    width  = 12;

    difference() {
        cube([len(labels) * width, 14, plate_t + 5]);

        for (i = [0 : len(labels) - 1])
            translate([width / 2 + i * width, 7, -eps])
                cylinder(d = labels[i], h = plate_t + 5 + 2 * eps);
    }
}

board_corner_gauge();

translate([22 + gap, 0, 0])
    servo_gauge();

translate([0, 22 + gap, 0])
    screw_gauge();
