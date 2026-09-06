// NOVA — head.
//
// The frame the display board sits in. Pivots between the yoke's arms: a
// servo-horn interface on one side, a plain axle stub on the other.
//
// Print orientation: face down on the bed, so the screen bezel is the first
// layer and comes out crisp. No supports; the board pocket is a simple
// upward cavity in this orientation.
//
//   openscad -o head.stl head.scad

include <params.scad>

pocket_w  = board_w + 2 * fit_clearance;
pocket_h  = board_h + 2 * fit_clearance;
pocket_t  = board_t + fit_clearance;

frame_w   = pocket_w + 2 * head_wall;
frame_h   = pocket_h + 2 * head_wall;
frame_t   = pocket_t + head_wall;   // wall is the front bezel

// The screen opening is deliberately a little smaller than the active area:
// the outermost pixels of an AMOLED sit under the bezel anyway, and a window
// cut oversize shows the board underneath.
window_w  = screen_w - 1.0;
window_h  = screen_h - 1.0;

module head_body() {
    // Rounded rectangle, to match the watch-shaped board.
    hull() {
        for (x = [-1, 1], y = [-1, 1])
            translate([x * (frame_w / 2 - 4), y * (frame_h / 2 - 4), 0])
                cylinder(r = 4, h = frame_t);
    }
}

module board_pocket() {
    // Cavity the board drops into, open at the back. Centred: an
    // uncentred cube here puts the pocket in one corner of the frame.
    translate([0, 0, head_wall + (pocket_t + eps) / 2])
        cube([pocket_w, pocket_h, pocket_t + eps], center = true);
}

module screen_window() {
    translate([0, 0, -eps])
        hull() {
            for (x = [-1, 1], y = [-1, 1])
                translate([x * (window_w / 2 - 2), y * (window_h / 2 - 2), 0])
                    cylinder(r = 2, h = head_wall + 2 * eps);
        }
}

module cable_channel() {
    // The board's tail leaves through the bottom edge.
    translate([0, -frame_h / 2, head_wall + pocket_t / 2])
        rotate([90, 0, 0])
            rounded_slot(12, 4.5, head_wall * 4);
}

module retaining_lip() {
    // Four tabs that stop the board falling out of the back, printed as part
    // of the frame. Small enough to flex the board past on assembly.
    for (x = [-1, 1])
        translate([x * pocket_w / 2, 0, head_wall + pocket_t])
            cube([1.6, pocket_h * 0.4, 1.2], center = true);
}

module pivot_features() {
    // Idle side: a stub that rides in the yoke's bearing hole.
    translate([frame_w / 2 - eps, 0, frame_t / 2])
        rotate([0, 90, 0])
            rotate([0, 0, 0])
                translate([0, 0, 0])
                    cylinder(d = axle_d, h = 5);
}

module horn_pocket() {
    // Driven side: a recess for the pitch servo's horn.
    translate([-frame_w / 2 - eps, 0, frame_t / 2])
        rotate([0, 90, 0])
            mirror([0, 0, 1]) {
                cylinder(d = 21.0, h = 2.2);
                cylinder(d = servo_shaft_d + 1.5, h = 6);
                for (a = [-1, 1])
                    translate([a * 7, 0, 0])
                        cylinder(d = 2.1, h = 6);
            }
}

module head() {
    difference() {
        union() {
            head_body();
            translate([0, 0, 0]) pivot_features();
        }

        board_pocket();
        screen_window();
        cable_channel();
        horn_pocket();
    }

    retaining_lip();
}

// Modelled face-up for readability; rotate in the slicer, or print as-is and
// accept a supported window.
head();
