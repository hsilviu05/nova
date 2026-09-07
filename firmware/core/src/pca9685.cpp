#include "nova/pca9685.hpp"

namespace nova::pca9685 {

namespace {

/// The four bytes a channel's ON/OFF registers take, little-endian.
///
/// Auto-increment is enabled in `configure`, so these land in LEDn_ON_L,
/// LEDn_ON_H, LEDn_OFF_L, LEDn_OFF_H from one starting register. Without
/// auto-increment every byte would need its own address and the servo would
/// receive a quarter of the value it was sent.
constexpr std::array<uint8_t, 4> channel_bytes(uint16_t on, uint16_t off) {
    return {
        static_cast<uint8_t>(on & 0xFF),
        static_cast<uint8_t>(on >> 8),
        static_cast<uint8_t>(off & 0xFF),
        static_cast<uint8_t>(off >> 8),
    };
}

constexpr uint8_t channel_register(uint8_t channel) {
    return static_cast<uint8_t>(kLed0OnLow + 4 * channel);
}

Sequence<1> channel_write(uint8_t channel, uint16_t on, uint16_t off) {
    Sequence<1> sequence;
    if (channel >= kChannels) {
        // Out of range writes nothing rather than scribbling over whatever
        // register the arithmetic lands on -- LED15_OFF_H is followed by
        // ALL_LED and then PRESCALE, so an overrun would reconfigure the
        // chip mid-flight.
        return sequence;
    }

    Step step;
    step.kind = Step::Kind::Write;
    step.reg = channel_register(channel);
    step.data = channel_bytes(on, off);
    step.length = 4;
    sequence.push(step);
    return sequence;
}

}  // namespace

Sequence<8> configure(int rate_hz) {
    Sequence<8> sequence;

    // Sleep first: PRESCALE is read-only while the oscillator runs, and the
    // chip accepts the write without complaint, leaving 200 Hz in place.
    sequence.push(Step::write(kMode1, kSleep | kAllCall));
    sequence.push(Step::write(kPrescale, pca9685_prescale(rate_hz)));

    // Wake, with auto-increment on so a channel's four bytes go in one write.
    sequence.push(Step::write(kMode1, kAutoIncrement | kAllCall));
    sequence.push(Step::wait(kOscillatorSettleUs));

    // RESTART resumes the PWM channels the sleep suspended.
    sequence.push(Step::write(kMode1, kRestart | kAutoIncrement | kAllCall));
    sequence.push(Step::write(kMode2, kOutputTotemPole));

    return sequence;
}

Sequence<1> set_channel(uint8_t channel, uint16_t on, uint16_t off) {
    return channel_write(channel, on, off);
}

Sequence<1> set_angle(uint8_t channel, int degrees) {
    const uint16_t off = pulse_us_to_ticks(angle_to_pulse_us(degrees));
    return channel_write(channel, 0, off);
}

Sequence<1> release(uint8_t channel) {
    // Bit 12 of the OFF register is "full off", which the datasheet gives
    // precedence over any ON value. Writing 0/0 instead would leave the
    // output permanently low rather than idle, which some servos read as a
    // malformed pulse and answer by jittering.
    return channel_write(channel, 0, 0x1000);
}

}  // namespace nova::pca9685
