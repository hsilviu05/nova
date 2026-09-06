// NOVA — yoke.
//
// Sits on the yaw servo's horn and carries the pitch servo. The head pivots
// between its two arms: one arm holds the servo, the other is a plain
// bearing for the idle side.
//
// Print orientation: as modelled, flat face down. No supports.
//
//   openscad -o yoke.stl yoke.scad

include <params.scad>

yoke_plate_t   = 3.6;
yoke_arm_t     = 4.0;
yoke_arm_h     = 26.0;
// Wide enough for the head to swing between the arms without rubbing.
yoke_span      = board_w + 2 * head_wall + 2 * 1.2;

horn_recess_d  = 21.0;   // SG90 round horn
horn_recess_t  = 2.2;
horn_screw_d   = 2.1;
horn_screw_span = 14.0;

module horn_interface() {
    // Recess for the horn itself.
    translate([0, 0, -eps])
        cylinder(d = horn_recess_d, h = horn_recess_t + eps);

    // Clearance for the servo's shaft boss, which stands proud of the horn.
    translate([0, 0, -eps])
        cylinder(d = servo_shaft_d + 1.5, h = yoke_plate_t + 2 * eps);

    // Two screws pulling the yoke down onto the horn. Two, not one: a single
    // central screw lets the whole head rotate on the horn under load.
    for (x = [-1, 1])
        translate([x * horn_screw_span / 2, 0, -eps])
            cylinder(d = horn_screw_d, h = yoke_plate_t + 2 * eps);
}

module servo_arm() {
    difference() {
        // The arm.
        translate([-servo_body_l / 2 - 3, -yoke_arm_t, 0])
            cube([servo_body_l + 6, yoke_arm_t, yoke_arm_h]);

        // Pocket the servo body drops into, shaft facing inward.
        translate([-servo_body_l / 2, -yoke_arm_t - eps, yoke_arm_h - servo_body_h - 2])
            cube([servo_body_l, yoke_arm_t + 2 * eps, servo_body_h + 2 + eps]);
    }
}

module bearing_arm() {
    difference() {
        translate([-10, 0, 0])
            cube([20, yoke_arm_t, yoke_arm_h]);

        // Plain bearing for the idle-side axle.
        translate([0, -eps, yoke_arm_h - 8])
            rotate([-90, 0, 0])
                cylinder(d = axle_d + axle_clearance, h = yoke_arm_t + 2 * eps);
    }
}

module yoke() {
    difference() {
        union() {
            // Base plate.
            hull() {
                for (y = [-1, 1])
                    translate([0, y * (yoke_span / 2 - 8), 0])
                        cylinder(d = 26, h = yoke_plate_t);
            }

            // Servo side.
            translate([0, -yoke_span / 2, yoke_plate_t - eps])
                mirror([0, 1, 0])
                    servo_arm();

            // Idle side.
            translate([0, yoke_span / 2 - yoke_arm_t, yoke_plate_t - eps])
                bearing_arm();
        }

        horn_interface();
    }
}

yoke();
