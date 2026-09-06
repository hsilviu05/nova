// Servo geometry.
//
// Arithmetic that ends up as a pulse width driving a physical mechanism.
// Getting it wrong does not throw an exception, it makes a servo grind
// against its stop until the gears strip -- so the limits get tested as
// carefully as the mapping.

#include "check.hpp"
#include "nova/geometry.hpp"

using namespace nova;

int main() {
    std::printf("geometry\n");

    CASE("centre maps to the middle of the pulse range") {
        // (500 + 2400) / 2 = 1450.
        CHECK_INT(angle_to_pulse_us(0), 1450);
    }

    CASE("the extremes map to the ends") {
        CHECK_INT(angle_to_pulse_us(-90), kServoMinPulseUs);
        CHECK_INT(angle_to_pulse_us(90), kServoMaxPulseUs);
    }

    CASE("the mapping is monotonic") {
        int previous = angle_to_pulse_us(-90);
        for (int degrees = -89; degrees <= 90; ++degrees) {
            const int pulse = angle_to_pulse_us(degrees);
            CHECK(pulse >= previous);
            previous = pulse;
        }
    }

    CASE("angles beyond the servo range are clamped, not wrapped") {
        // Wrapping would send a request for 200 degrees to the far end of
        // travel at speed. Clamping stops at the limit.
        CHECK_INT(angle_to_pulse_us(200), kServoMaxPulseUs);
        CHECK_INT(angle_to_pulse_us(-200), kServoMinPulseUs);
    }

    CASE("a head pose cannot be constructed out of range") {
        const auto pose = HeadPose::clamped(150, -80);
        CHECK_INT(pose.yaw, kMaxYaw);
        CHECK_INT(pose.pitch, kMinPitch);
    }

    CASE("an in-range pose is untouched") {
        const auto pose = HeadPose::clamped(30, -20);
        CHECK_INT(pose.yaw, 30);
        CHECK_INT(pose.pitch, -20);
    }

    CASE("pitch has a tighter limit than yaw") {
        // The head can turn much further than it can nod; the mechanism
        // simply has less room vertically.
        CHECK(kMaxPitch < kMaxYaw);
        CHECK_INT(HeadPose::clamped(0, 60).pitch, kMaxPitch);
        CHECK_INT(HeadPose::clamped(60, 0).yaw, 60);
    }

    CASE("pulse widths convert to PCA9685 ticks") {
        // 1450 us of a 20000 us period, over 4096 steps: 296.
        CHECK_INT(pulse_us_to_ticks(1450), 296);
        CHECK_INT(pulse_us_to_ticks(kServoMinPulseUs), 102);
        CHECK_INT(pulse_us_to_ticks(kServoMaxPulseUs), 491);
    }

    CASE("ticks never reach the counter's wrap point") {
        // The PCA9685 counter runs 0-4095. A value of 4096 wraps to 0 and
        // turns the channel fully off, which reads as a dead servo.
        CHECK(pulse_us_to_ticks(kServoPeriodUs) < kPwmSteps);
        CHECK(pulse_us_to_ticks(999999) < kPwmSteps);
    }

    CASE("the prescale for 50 Hz is the datasheet value") {
        // 25 MHz / (4096 * 50) - 1 = 121.07, rounded to 121.
        CHECK_INT(pca9685_prescale(50), 121);
    }

    CASE("the prescale rounds rather than truncating") {
        // Truncating here puts a 50 Hz request at about 51.9 Hz, shifting
        // every pulse width by roughly 4% -- a systematic angle error that
        // looks like a badly calibrated servo.
        CHECK_INT(pca9685_prescale(1000), 5);
        // 25e6 / (4096 * 60) = 101.72, which rounds to 102, less one is 101.
        // Truncating gives 100 and a 1% frequency error.
        CHECK_INT(pca9685_prescale(60), 101);
    }

    CASE("the prescale stays within the register's legal range") {
        CHECK(pca9685_prescale(100000) >= 3);
        CHECK_INT(pca9685_prescale(1), 255);
    }

    return check::report("geometry");
}
