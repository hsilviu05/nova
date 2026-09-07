// Reading the rangefinder without being lied to.
//
// The sensor reports failure in-band, so the interesting cases are the ones
// where a number arrives that is not a distance.

#include <optional>

#include "check.hpp"
#include "nova/rangefinder.hpp"

using namespace nova;

namespace {

/// A RESULT_RANGE_STATUS byte carrying `status` in bits 3..6.
constexpr uint8_t status_byte(uint8_t status) {
    return static_cast<uint8_t>(status << kRangeStatusShift);
}

void test_interpretation() {
    CASE("a valid measurement comes back as a distance") {
        const RangeReading reading = interpret(status_byte(kRangeStatusValid), 450);
        CHECK(reading.quality == RangeQuality::Good);
        CHECK_INT(reading.millimetres, 450);
        CHECK(reading.usable());
    }

    CASE("any status but valid is a failure, whatever the number says") {
        // The number is present and plausible in every one of these. Reading
        // it without the status is how a device ends up lurching at a wall.
        for (uint8_t status = 0; status < 16; ++status) {
            if (status == kRangeStatusValid) {
                continue;
            }
            const RangeReading reading = interpret(status_byte(status), 450);
            CHECK(reading.quality == RangeQuality::Failed);
            CHECK(!reading.usable());
        }
    }

    CASE("the status is read from bits 3..6, not the whole byte") {
        // The low bits carry unrelated flags. A naive equality test against
        // the raw byte rejects every good measurement on a real part.
        const uint8_t with_low_bits = static_cast<uint8_t>(status_byte(kRangeStatusValid) | 0x07);
        CHECK(interpret(with_low_bits, 450).usable());

        const uint8_t with_high_bit = static_cast<uint8_t>(status_byte(kRangeStatusValid) | 0x80);
        CHECK(interpret(with_high_bit, 450).usable());
    }

    CASE("beyond the reliable range is 'nothing there', not a failure") {
        // An empty desk is information; a broken sensor is not. The behaviour
        // engine treats them differently and must be able to.
        const RangeReading reading = interpret(status_byte(kRangeStatusValid), 4000);
        CHECK(reading.quality == RangeQuality::OutOfRange);
        CHECK(!reading.usable());

        CHECK(interpret(status_byte(kRangeStatusValid), kMaxReliableMm).usable());
        CHECK(!interpret(status_byte(kRangeStatusValid), kMaxReliableMm + 1).usable());
    }

    CASE("contact range is clamped, not believed") {
        // Under a few centimetres the return is cover-glass cross-talk. The
        // object is real; the 4 mm is not.
        const RangeReading reading = interpret(status_byte(kRangeStatusValid), 4);
        CHECK(reading.quality == RangeQuality::Good);
        CHECK_INT(reading.millimetres, kMinReliableMm);
    }

    CASE("a saturated reading does not wrap into the near field") {
        // 8190 is the part's saturation sentinel. Truncating it to 16 bits
        // somewhere upstream would put it well inside the presence threshold.
        CHECK(!interpret(status_byte(kRangeStatusValid), 8190).usable());
        CHECK(!interpret(status_byte(kRangeStatusValid), 8191).usable());
    }
}

void test_units() {
    CASE("millimetres round to centimetres") {
        CHECK_INT(to_centimetres(0), 0);
        CHECK_INT(to_centimetres(4), 0);
        CHECK_INT(to_centimetres(5), 1);
        CHECK_INT(to_centimetres(704), 70);
        CHECK_INT(to_centimetres(705), 71);
    }

    CASE("rounding does not bias the presence threshold downward") {
        // Truncation would put everything from 700 to 709 mm at 70 cm, which
        // moves the line at which NOVA decides someone arrived by a whole
        // centimetre in one direction.
        CHECK_INT(to_centimetres(699), 70);
        CHECK_INT(to_centimetres(700), 70);
    }
}

void test_filter() {
    CASE("the filter reports nothing until its window fills") {
        // Otherwise the first reading after boot is its own median, and one
        // outlier decides that somebody is there.
        RangeFilter<5> filter;
        for (int i = 0; i < 4; ++i) {
            CHECK(!filter.ready());
            CHECK(filter.value() == std::nullopt);
            filter.push(300);
        }
        filter.push(300);
        CHECK(filter.ready());
        CHECK(filter.value() == std::optional<uint16_t>{300});
    }

    CASE("a single outlier does not move the median") {
        // The whole reason this is a median and not an average: a mean would
        // move 120 mm here, which is a sleeve passing the aperture becoming
        // NOVA looking up.
        RangeFilter<5> filter;
        filter.push(300);
        filter.push(305);
        filter.push(900);
        filter.push(298);
        filter.push(302);
        CHECK(filter.value() == std::optional<uint16_t>{302});
    }

    CASE("a sustained change moves the median once it is the majority") {
        // A filter that rejects outliers but also rejects real movement is
        // just a constant. A median of five flips when three of the five
        // agree -- two readings of a new distance are still an outlier, three
        // are a person.
        RangeFilter<5> filter;
        for (int i = 0; i < 5; ++i) {
            filter.push(800);
        }
        CHECK(filter.value() == std::optional<uint16_t>{800});

        filter.push(300);
        filter.push(300);
        CHECK(filter.value() == std::optional<uint16_t>{800});

        filter.push(300);
        CHECK(filter.value() == std::optional<uint16_t>{300});
    }

    CASE("the window slides rather than filling once") {
        RangeFilter<3> filter;
        filter.push(100);
        filter.push(200);
        filter.push(300);
        CHECK(filter.value() == std::optional<uint16_t>{200});

        filter.push(400);
        // Oldest sample gone: 200, 300, 400.
        CHECK(filter.value() == std::optional<uint16_t>{300});
    }

    CASE("reset makes the filter wait for a fresh window") {
        // Readings from before a gap describe a room that has had time to
        // change, so none of them may reach the next median.
        RangeFilter<3> filter;
        filter.push(900);
        filter.push(900);
        filter.push(900);
        filter.reset();
        CHECK(!filter.ready());

        filter.push(100);
        filter.push(110);
        CHECK(filter.value() == std::nullopt);
        filter.push(105);
        CHECK(filter.value() == std::optional<uint16_t>{105});
    }

    CASE("the filter never sorts in place") {
        // value() is const and must stay so: sorting the live buffer would
        // scramble the ring's ordering and break the next slide.
        RangeFilter<3> filter;
        filter.push(300);
        filter.push(100);
        filter.push(200);
        CHECK(filter.value() == std::optional<uint16_t>{200});
        CHECK(filter.value() == std::optional<uint16_t>{200});

        filter.push(400);
        // If the buffer had been sorted, the oldest slot would no longer hold
        // 300 and this would come back as something else.
        CHECK(filter.value() == std::optional<uint16_t>{200});
    }
}

void test_pipeline() {
    CASE("failed readings never reach the filter") {
        // Pushing a failure as zero would read as someone leaning into the
        // sensor. The caller only pushes usable readings; this is the shape
        // of that contract.
        RangeFilter<3> filter;
        const uint8_t raw[] = {
            status_byte(kRangeStatusValid),
            status_byte(4),  // failure
            status_byte(kRangeStatusValid),
            status_byte(kRangeStatusValid),
        };
        const uint16_t mm[] = {600, 0, 610, 605};

        for (size_t i = 0; i < 4; ++i) {
            const RangeReading reading = interpret(raw[i], mm[i]);
            if (reading.usable()) {
                filter.push(reading.millimetres);
            }
        }
        CHECK(filter.ready());
        CHECK_INT(to_centimetres(*filter.value()), 61);
    }
}

}  // namespace

int main() {
    std::printf("rangefinder\n");
    test_interpretation();
    test_units();
    test_filter();
    test_pipeline();
    return check::report("rangefinder");
}
