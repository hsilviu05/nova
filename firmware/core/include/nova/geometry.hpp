// Servo geometry: turning an intent into a pulse width, safely.
//
// Pure arithmetic, no hardware. This is the layer that decides what the
// mechanism is physically allowed to do, and it is separated from the driver
// so that "can the head reach this angle" is answerable without a servo
// attached to anything.

#pragma once

#include <cstdint>

namespace nova {

// Travel limits, matching schemas/protocol.py. The mechanism cannot reach
// beyond these; a command asking it to is rejected at the API boundary, and
// clamped here as the last line of defence.
inline constexpr int kMinYaw = -90;
inline constexpr int kMaxYaw = 90;
inline constexpr int kMinPitch = -45;
inline constexpr int kMaxPitch = 45;

// SG90 pulse widths in microseconds. The nominal 1000-2000 range under-uses
// the travel; 500-2400 is what these actually do, and is what the datasheet
// for the clones states.
inline constexpr int kServoMinPulseUs = 500;
inline constexpr int kServoMaxPulseUs = 2400;
inline constexpr int kServoPeriodUs = 20000;  // 50 Hz

// PCA9685 resolution: each channel is a 12-bit position in the period.
inline constexpr int kPwmSteps = 4096;

/// Clamp to a closed range.
constexpr int clamp(int value, int low, int high) {
    return value < low ? low : (value > high ? high : value);
}

/// A head position, in degrees.
///
/// Constructed clamped, so an out-of-range angle cannot exist as a value.
/// That matters more than it looks: this type is what reaches the driver, so
/// making the illegal state unrepresentable removes a whole class of "the
/// servo buzzed against its stop all night" bug.
struct HeadPose {
    int yaw = 0;
    int pitch = 0;

    static constexpr HeadPose clamped(int yaw, int pitch) {
        return HeadPose{clamp(yaw, kMinYaw, kMaxYaw), clamp(pitch, kMinPitch, kMaxPitch)};
    }

    constexpr bool operator==(const HeadPose &other) const {
        return yaw == other.yaw && pitch == other.pitch;
    }
};

/// Map an angle in [-90, 90] to a pulse width in microseconds.
///
/// Linear. A real SG90 is not quite linear across its travel, but the error
/// is a couple of degrees at the extremes and calibrating it needs a
/// protractor and the actual servo -- which is a job for when hardware
/// exists, not a guess to bake in now.
constexpr int angle_to_pulse_us(int degrees) {
    const int clamped_degrees = clamp(degrees, -90, 90);
    const int span = kServoMaxPulseUs - kServoMinPulseUs;
    // +90 shifts [-90, 90] to [0, 180] before scaling.
    return kServoMinPulseUs + (clamped_degrees + 90) * span / 180;
}

/// Convert a pulse width to a PCA9685 off-tick.
///
/// Every channel turns on at tick 0 and off at the returned tick. Staggering
/// the on-ticks would spread the current draw, which matters with many
/// servos; with two it costs clarity for nothing measurable.
constexpr uint16_t pulse_us_to_ticks(int pulse_us) {
    const int ticks = pulse_us * kPwmSteps / kServoPeriodUs;
    // The counter wraps at 4095, so a full-scale value has to be held below.
    return static_cast<uint16_t>(clamp(ticks, 0, kPwmSteps - 1));
}

/// The PCA9685 prescale value for a target frequency.
///
/// From the datasheet: prescale = round(osc / (4096 * rate)) - 1, with a
/// 25 MHz internal oscillator.
constexpr uint8_t pca9685_prescale(int rate_hz, int oscillator_hz = 25000000) {
    const int divisor = kPwmSteps * rate_hz;
    // Rounded, not truncated: truncating puts a 50 Hz request at 51.9 Hz,
    // which shifts every pulse width by 4%.
    const int prescale = (oscillator_hz + divisor / 2) / divisor - 1;
    return static_cast<uint8_t>(clamp(prescale, 3, 255));
}

}  // namespace nova
