// NOVA — base.
//
// Holds the yaw servo, the PCA9685, the wiring, and the time-of-flight
// sensor. Everything heavy lives down here so the head stays light enough
// for an SG90 to move without overshooting.
//
// Print orientation: as modelled, open side up. No supports needed -- the
// only overhangs are the vent slots and the cable exit, both of which are
// bridges under 10 mm.
//
//   openscad -o base.stl base.scad

include <params.scad>

// The servo sits centred, shaft up, so the head rotates about the base axis.
servo_pocket_depth = servo_body_h + 1.0;

module base_shell() {
    // Slightly conical: a straight cylinder reads as a tin can, and the taper
    // also lifts the first layer off the bed edge cleanly.
    cylinder(d1 = base_d, d2 = base_d - base_taper, h = base_h);
}

module servo_cutout() {
    // Centred on the base axis, with the output shaft on the axis rather
    // than the body -- otherwise the head orbits instead of turning.
    translate([-servo_shaft_offset, -servo_body_w / 2, 0]) {
        translate([0, 0, base_h - servo_pocket_depth])
            cube([servo_body_l, servo_body_w, servo_pocket_depth + eps]);

        // Mounting tabs, recessed so the servo sits flush with the top face.
        translate([
            -(servo_tab_l - servo_body_l) / 2,
            0,
            base_h - servo_tab_t - 1.2,
        ])
            cube([servo_tab_l, servo_body_w, servo_tab_t + 1.2 + eps]);
    }

    // Screw holes through the tabs.
    for (x = [-1, 1])
        translate([
            -servo_shaft_offset + servo_body_l / 2 + x * servo_tab_span / 2,
            0,
            base_h - servo_tab_t - 6,
        ])
            cylinder(d = m2_pilot_d, h = 10);
}

module tof_cutout() {
    // Front face, angled slightly downward: the thing it is looking for is a
    // person at a desk, which is below the sensor, not level with it.
    translate([0, -base_d / 2 + wall / 2, base_h * 0.55])
        rotate([90 - 8, 0, 0]) {
            // Window the laser looks through.
            translate([0, 0, -wall])
                cylinder(d = tof_window_d, h = wall * 4, center = true);

            // Pocket for the breakout, from the inside.
            translate([0, -(tof_pcb_h / 2 - tof_sensor_y), -wall - tof_pcb_t / 2 - 1.2])
                cube(
                    [
                        tof_pcb_w + fit_clearance * 2,
                        tof_pcb_h + fit_clearance * 2,
                        tof_pcb_t + 2.4,
                    ],
                    center = true
                );
        }
}

module cable_exit() {
    translate([0, base_d / 2 - wall, floor_t + cable_slot_h / 2 + 1])
        rotate([90, 0, 0])
            rounded_slot(cable_slot_w, cable_slot_h, wall * 4);
}

module vents() {
    // Six slots around the lower wall. The servo and the regulator both make
    // heat, and a sealed box is how a desk object becomes a warm desk object.
    for (angle = [0 : 60 : 359])
        rotate([0, 0, angle])
            translate([0, base_d / 2 - wall, base_h * 0.28])
                rotate([90, 0, 0])
                    rounded_slot(18, 3.2, wall * 4);
}

module pcb_standoffs() {
    // For the PCA9685: 62.5 x 25.4 mm, holes 3 mm in from each corner.
    hole_dx = 62.5 - 6.0;
    hole_dy = 25.4 - 6.0;

    for (x = [-1, 1], y = [-1, 1])
        translate([x * hole_dx / 2, y * hole_dy / 2 - 12, floor_t - eps])
            screw_boss(4.0);
}

module base() {
    difference() {
        base_shell();

        // Hollow it out, leaving the floor and walls.
        translate([0, 0, floor_t])
            cylinder(
                d1 = base_d - 2 * wall,
                d2 = base_d - base_taper - 2 * wall,
                h = base_h
            );

        servo_cutout();
        tof_cutout();
        cable_exit();
        vents();
    }

    pcb_standoffs();
}

base();
