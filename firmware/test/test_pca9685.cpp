// The PCA9685 bring-up sequence, checked byte by byte.
//
// These tests exist because this chip fails quietly. Write PRESCALE while the
// oscillator is running and the chip accepts the write, ignores it, and keeps
// running at 200 Hz -- there is no error, no NACK, nothing to see except
// servos that hum and never quite arrive. The only place to catch that is
// here, in the encoded order of the writes.

#include "check.hpp"
#include "nova/pca9685.hpp"

using namespace nova;
using nova::pca9685::Step;

namespace {

/// Find the index of the first write to `reg`, or -1.
int index_of_write(const auto &sequence, uint8_t reg) {
    for (size_t i = 0; i < sequence.size(); ++i) {
        if (sequence[i].kind == Step::Kind::Write && sequence[i].reg == reg) {
            return static_cast<int>(i);
        }
    }
    return -1;
}

void test_configure_order() {
    CASE("configure sleeps before writing PRESCALE") {
        const auto sequence = pca9685::configure(50);

        const int prescale_at = index_of_write(sequence, pca9685::kPrescale);
        CHECK(prescale_at > 0);

        // Every write before PRESCALE must leave the chip asleep, and the one
        // immediately before it is the one that matters.
        const Step &before = sequence[static_cast<size_t>(prescale_at) - 1];
        CHECK_INT(before.reg, pca9685::kMode1);
        CHECK_INT(before.data[0] & pca9685::kSleep, pca9685::kSleep);
    }

    CASE("configure wakes only after PRESCALE is set") {
        const auto sequence = pca9685::configure(50);
        const int prescale_at = index_of_write(sequence, pca9685::kPrescale);

        // After PRESCALE there is exactly one MODE1 write that clears SLEEP,
        // and it comes after, not before.
        bool woke = false;
        for (size_t i = 0; i < sequence.size(); ++i) {
            const Step &step = sequence[i];
            if (step.kind != Step::Kind::Write || step.reg != pca9685::kMode1) {
                continue;
            }
            if ((step.data[0] & pca9685::kSleep) == 0) {
                woke = true;
                CHECK(static_cast<int>(i) > prescale_at);
            }
        }
        CHECK(woke);
    }

    CASE("configure waits for the oscillator before RESTART") {
        const auto sequence = pca9685::configure(50);

        int delay_at = -1;
        int restart_at = -1;
        for (size_t i = 0; i < sequence.size(); ++i) {
            const Step &step = sequence[i];
            if (step.kind == Step::Kind::Delay && delay_at < 0) {
                delay_at = static_cast<int>(i);
                CHECK_INT(step.delay_us, pca9685::kOscillatorSettleUs);
            }
            if (step.kind == Step::Kind::Write && step.reg == pca9685::kMode1 &&
                (step.data[0] & pca9685::kRestart) != 0) {
                restart_at = static_cast<int>(i);
            }
        }
        CHECK(delay_at >= 0);
        CHECK(restart_at > delay_at);
    }

    CASE("configure enables auto-increment before any channel write") {
        // Without AUTO_INCREMENT a four-byte channel write lands entirely in
        // LEDn_ON_L, so the servo receives a quarter of the value it was sent.
        const auto sequence = pca9685::configure(50);

        bool auto_increment = false;
        for (size_t i = 0; i < sequence.size(); ++i) {
            const Step &step = sequence[i];
            if (step.kind == Step::Kind::Write && step.reg == pca9685::kMode1 &&
                (step.data[0] & pca9685::kSleep) == 0) {
                auto_increment = (step.data[0] & pca9685::kAutoIncrement) != 0;
                break;
            }
        }
        CHECK(auto_increment);
    }

    CASE("configure sets totem-pole output") {
        const auto sequence = pca9685::configure(50);
        const int mode2_at = index_of_write(sequence, pca9685::kMode2);
        CHECK(mode2_at >= 0);
        CHECK_INT(sequence[static_cast<size_t>(mode2_at)].data[0] & pca9685::kOutputTotemPole,
                  pca9685::kOutputTotemPole);
    }

    CASE("configure carries the prescale for the requested rate") {
        const auto fifty = pca9685::configure(50);
        const auto sixty = pca9685::configure(60);

        const int a = index_of_write(fifty, pca9685::kPrescale);
        const int b = index_of_write(sixty, pca9685::kPrescale);

        CHECK_INT(fifty[static_cast<size_t>(a)].data[0], pca9685_prescale(50));
        CHECK_INT(sixty[static_cast<size_t>(b)].data[0], pca9685_prescale(60));
        // Different rates must actually produce different bytes, or the
        // parameter is decorative.
        CHECK(fifty[static_cast<size_t>(a)].data[0] != sixty[static_cast<size_t>(b)].data[0]);
    }

    CASE("every configure write is a single byte") {
        const auto sequence = pca9685::configure(50);
        for (size_t i = 0; i < sequence.size(); ++i) {
            if (sequence[i].kind == Step::Kind::Write) {
                CHECK_INT(sequence[i].length, 1);
            }
        }
    }
}

void test_channel_writes() {
    CASE("set_channel targets the right register block") {
        // LED0 at 0x06, four registers per channel.
        CHECK_INT(pca9685::set_channel(0, 0, 100)[0].reg, 0x06);
        CHECK_INT(pca9685::set_channel(1, 0, 100)[0].reg, 0x0A);
        CHECK_INT(pca9685::set_channel(15, 0, 100)[0].reg, 0x42);
    }

    CASE("set_channel encodes on and off little-endian") {
        const auto sequence = pca9685::set_channel(0, 0x0123, 0x0456);
        CHECK_INT(sequence.size(), 1);
        CHECK_INT(sequence[0].length, 4);
        CHECK_INT(sequence[0].data[0], 0x23);
        CHECK_INT(sequence[0].data[1], 0x01);
        CHECK_INT(sequence[0].data[2], 0x56);
        CHECK_INT(sequence[0].data[3], 0x04);
    }

    CASE("an out-of-range channel writes nothing") {
        // LED15_OFF_H is followed by ALL_LED and then PRESCALE. An overrun
        // here would silently reconfigure the chip mid-flight, so the write
        // is refused rather than clamped to channel 15 -- clamping would move
        // the wrong servo, which is worse than moving none.
        CHECK_INT(pca9685::set_channel(16, 0, 100).size(), 0);
        CHECK_INT(pca9685::set_channel(255, 0, 100).size(), 0);
        CHECK_INT(pca9685::set_angle(16, 0).size(), 0);
        CHECK_INT(pca9685::release(16).size(), 0);
    }

    CASE("set_angle composes the geometry") {
        const auto centred = pca9685::set_angle(0, 0);
        const uint16_t expected = pulse_us_to_ticks(angle_to_pulse_us(0));
        const uint16_t actual = static_cast<uint16_t>(centred[0].data[2] |
                                                      (centred[0].data[3] << 8));
        CHECK_INT(actual, expected);
        // Rises at tick 0.
        CHECK_INT(centred[0].data[0], 0);
        CHECK_INT(centred[0].data[1], 0);
    }

    CASE("set_angle clamps beyond travel rather than wrapping") {
        const auto past_limit = pca9685::set_angle(0, 400);
        const auto at_limit = pca9685::set_angle(0, 90);
        CHECK(past_limit[0] == at_limit[0]);

        const auto below = pca9685::set_angle(0, -400);
        const auto at_low = pca9685::set_angle(0, -90);
        CHECK(below[0] == at_low[0]);
    }

    CASE("set_angle stays within the 12-bit counter") {
        for (int degrees = -90; degrees <= 90; ++degrees) {
            const auto sequence = pca9685::set_angle(0, degrees);
            const uint16_t off = static_cast<uint16_t>(sequence[0].data[2] |
                                                       (sequence[0].data[3] << 8));
            CHECK(off < kPwmSteps);
        }
    }

    CASE("release sets the full-off bit, not zero") {
        // Bit 12 of OFF is "full off" and the datasheet gives it precedence.
        // Writing 0/0 instead holds the line low, which some servos read as a
        // malformed pulse and answer by jittering all night.
        const auto sequence = pca9685::release(3);
        CHECK_INT(sequence.size(), 1);
        CHECK_INT(sequence[0].reg, 0x06 + 4 * 3);
        CHECK_INT(sequence[0].data[3], 0x10);
    }
}

void test_sequence_container() {
    CASE("a sequence never overruns its capacity") {
        pca9685::Sequence<2> sequence;
        sequence.push(Step::write(0x00, 1));
        sequence.push(Step::write(0x00, 2));
        sequence.push(Step::write(0x00, 3));
        CHECK_INT(sequence.size(), 2);
        CHECK_INT(sequence[1].data[0], 2);
    }
}

}  // namespace

int main() {
    std::printf("pca9685\n");
    test_configure_order();
    test_channel_writes();
    test_sequence_container();
    return check::report("pca9685");
}
