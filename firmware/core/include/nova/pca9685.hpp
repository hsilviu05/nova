// PCA9685 command encoding.
//
// The board exposes no free PWM GPIO, so every servo goes through this chip
// on I²C -- see ADR 008. What lives here is the *encoding*: which registers,
// which bytes, in which order, with which delays. What does not live here is
// any actual I²C traffic, which is `main/`'s job.
//
// The split is not ceremony. Getting the initialisation order wrong on this
// chip produces servos that twitch, hum, or sit at the wrong angle, and none
// of those failures announce themselves as a bug in a specific line. Encoded
// here, the whole sequence is a value a test can inspect byte by byte.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "nova/geometry.hpp"

namespace nova::pca9685 {

// Default address with every address jumper open.
inline constexpr uint8_t kDefaultAddress = 0x40;

// Registers, from the datasheet.
inline constexpr uint8_t kMode1 = 0x00;
inline constexpr uint8_t kMode2 = 0x01;
inline constexpr uint8_t kLed0OnLow = 0x06;
inline constexpr uint8_t kPrescale = 0xFE;

// MODE1 bits.
inline constexpr uint8_t kRestart = 0x80;
inline constexpr uint8_t kAutoIncrement = 0x20;
inline constexpr uint8_t kSleep = 0x10;
inline constexpr uint8_t kAllCall = 0x01;

// MODE2 bits. Totem-pole output: the servo signal line is driven both ways
// rather than pulled up, which is what a servo's input expects.
inline constexpr uint8_t kOutputTotemPole = 0x04;

// The oscillator needs this long to stabilise after SLEEP is cleared.
// Skipping it is the classic cause of a first movement that jumps.
inline constexpr uint32_t kOscillatorSettleUs = 500;

inline constexpr size_t kChannels = 16;

/// One register write, or a pause.
///
/// A sequence of these is what `main/` replays onto the bus. Modelling the
/// delay as a step rather than leaving it to the caller means the ordering
/// requirement is part of the value being tested, not a comment somebody has
/// to remember.
struct Step {
    enum class Kind : uint8_t { Write, Delay };

    Kind kind = Kind::Write;
    uint8_t reg = 0;
    // Register writes on this chip are 1 or 4 bytes; nothing needs more.
    std::array<uint8_t, 4> data{};
    uint8_t length = 0;
    uint32_t delay_us = 0;

    static constexpr Step write(uint8_t reg, uint8_t value) {
        Step step;
        step.kind = Kind::Write;
        step.reg = reg;
        step.data = {value, 0, 0, 0};
        step.length = 1;
        return step;
    }

    static constexpr Step wait(uint32_t microseconds) {
        Step step;
        step.kind = Kind::Delay;
        step.delay_us = microseconds;
        return step;
    }

    constexpr bool operator==(const Step &other) const {
        return kind == other.kind && reg == other.reg && length == other.length &&
               delay_us == other.delay_us && data == other.data;
    }
};

/// A fixed-capacity sequence. No heap: this runs on a microcontroller, and a
/// vector here would allocate on every servo command.
template <size_t Capacity>
struct Sequence {
    std::array<Step, Capacity> steps{};
    size_t count = 0;

    constexpr void push(const Step &step) {
        if (count < Capacity) {
            steps[count++] = step;
        }
    }

    constexpr const Step &operator[](size_t index) const { return steps[index]; }
    constexpr size_t size() const { return count; }
};

/// The bring-up sequence for a given PWM rate.
///
/// Order is dictated by the datasheet and is not negotiable:
///
///   1. sleep, because PRESCALE is only writable while the oscillator is off
///   2. write PRESCALE
///   3. wake
///   4. wait for the oscillator
///   5. RESTART, which resumes the PWM channels
///
/// Writing PRESCALE without sleeping first is silently ignored by the chip,
/// which leaves it at the power-on default of 200 Hz -- servos fed a 200 Hz
/// signal buzz and never reach the commanded angle.
Sequence<8> configure(int rate_hz = 50);

/// Set one channel's pulse.
///
/// `on` is the tick the pulse rises, `off` the tick it falls. Everything here
/// rises at 0; staggering the on-ticks would spread current draw across the
/// period, which matters with a dozen servos and costs clarity with two.
Sequence<1> set_channel(uint8_t channel, uint16_t on, uint16_t off);

/// Set one channel from an angle in degrees.
///
/// Composes the geometry: degrees to pulse width to ticks. The clamping in
/// `angle_to_pulse_us` is the last guard before a real servo, so an angle
/// outside travel becomes the limit rather than a mechanical stall.
Sequence<1> set_angle(uint8_t channel, int degrees);

/// Stop driving a channel entirely.
///
/// Not the same as commanding an angle. A servo held at a position draws
/// current and hums; a servo with no signal goes limp. Used when the device
/// sleeps, so a desk object is not audibly straining all night.
Sequence<1> release(uint8_t channel);

}  // namespace nova::pca9685
