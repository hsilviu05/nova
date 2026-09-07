// Turning VL53L0X readings into something the behaviour engine can trust.
//
// The sensor is what emits `person_detected` -- there is no camera, by
// decision (ADR 008) -- so everything downstream of it inherits its noise.
// Two properties of the part shape this file:
//
//   * **It reports failure in-band.** A measurement that timed out, saturated
//     on a reflective surface, or found nothing comes back as a number
//     alongside a status byte. Reading the number without the status is the
//     classic way to get a device that lurches at a blank wall.
//
//   * **It produces occasional wild outliers.** Ambient infrared, a glossy
//     desk, a sleeve passing the aperture: single readings hundreds of
//     millimetres off, surrounded by good ones. An average smears those
//     across the window; a median discards them outright.
//
// What is NOT here is the sensor's initialisation. ST's sequence is roughly
// two hundred register writes covering SPAD calibration and a tuning set that
// exists only in their driver, and reproducing it from memory would be
// guesswork sitting under a presence signal the whole product depends on. The
// device layer uses ST's own driver for bring-up; what this file owns is the
// part that decides what a reading *means*, which is the part with judgement
// in it and therefore the part worth testing.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

namespace nova {

/// RESULT_RANGE_STATUS, 0x14. The status occupies bits 3..6.
inline constexpr uint8_t kRangeStatusRegister = 0x14;
inline constexpr uint8_t kRangeStatusMask = 0x78;
inline constexpr uint8_t kRangeStatusShift = 3;

/// The one status value ST's driver treats as a good measurement. Every other
/// code is some flavour of "the number next to me is not a distance".
inline constexpr uint8_t kRangeStatusValid = 11;

/// IDENTIFICATION_MODEL_ID and the value a real VL53L0X returns.
///
/// Checked at bring-up: the clone modules vary in silk-screen and regulator
/// but not in this register, and a bus with nothing on it reads 0x00 or 0xFF
/// rather than failing, so without this check a missing sensor looks like a
/// sensor that never sees anyone.
inline constexpr uint8_t kModelIdRegister = 0xC0;
inline constexpr uint8_t kModelId = 0xEE;

/// Beyond this the part's own accuracy claim lapses in default mode.
///
/// NOVA cares about a metre of desk, so the extra range of long-range mode is
/// not worth the ambient-light sensitivity it trades for.
inline constexpr uint16_t kMaxReliableMm = 1200;

/// Under a few centimetres the return is dominated by cross-talk from the
/// cover glass. A reading here means something is touching the sensor, not
/// that it is 4 mm away.
inline constexpr uint16_t kMinReliableMm = 30;

/// What a single measurement was worth.
enum class RangeQuality : uint8_t {
    /// A distance, within the range the part can back up.
    Good,
    /// The measurement succeeded and found nothing near enough to matter.
    /// Distinct from a failure: "the desk is empty" is information.
    OutOfRange,
    /// The sensor said the measurement failed. No distance to be had.
    Failed,
};

struct RangeReading {
    RangeQuality quality = RangeQuality::Failed;
    /// Only meaningful when quality is Good.
    uint16_t millimetres = 0;

    constexpr bool usable() const { return quality == RangeQuality::Good; }
};

/// Interpret one raw measurement.
///
/// `status_byte` is RESULT_RANGE_STATUS as read from the bus, not a decoded
/// status: decoding it here keeps the shift in one place rather than in
/// whichever call site remembered it.
constexpr RangeReading interpret(uint8_t status_byte, uint16_t millimetres) {
    const uint8_t status = static_cast<uint8_t>((status_byte & kRangeStatusMask) >>
                                                kRangeStatusShift);
    if (status != kRangeStatusValid) {
        return RangeReading{RangeQuality::Failed, 0};
    }
    if (millimetres > kMaxReliableMm) {
        return RangeReading{RangeQuality::OutOfRange, 0};
    }
    if (millimetres < kMinReliableMm) {
        // Cross-talk, not proximity. Reported as a contact-range reading
        // rather than discarded, because something is genuinely there.
        return RangeReading{RangeQuality::Good, kMinReliableMm};
    }
    return RangeReading{RangeQuality::Good, millimetres};
}

/// Millimetres to the centimetres the behaviour engine speaks.
///
/// Rounds rather than truncates: at the 70 cm presence threshold, truncation
/// biases every reading downward by up to a centimetre, which moves the line
/// at which NOVA decides someone arrived.
constexpr int to_centimetres(uint16_t millimetres) {
    return static_cast<int>((millimetres + 5) / 10);
}

/// A rolling median over the last `Window` usable readings.
///
/// Median rather than mean, and this is the whole reason the filter exists: a
/// single 900 mm outlier in a window of 300 mm readings moves a three-sample
/// mean by 200 mm and a three-sample median by nothing at all. Averaging
/// would turn a sleeve passing the aperture into NOVA looking up.
///
/// Failed readings are not pushed. Feeding them in as zeroes would let a
/// sensor fault read as someone leaning in.
template <size_t Window = 5>
class RangeFilter {
   public:
    static_assert(Window % 2 == 1, "an even window has no single median");
    static_assert(Window > 0, "a filter over nothing filters nothing");

    void push(uint16_t millimetres) {
        samples_[next_] = millimetres;
        next_ = (next_ + 1) % Window;
        if (filled_ < Window) {
            ++filled_;
        }
    }

    /// The current median, or nothing until the window has filled.
    ///
    /// Waiting for a full window costs a few hundred milliseconds at startup
    /// and avoids the alternative, where the first reading after boot is
    /// itself the median and a single outlier decides that someone is there.
    std::optional<uint16_t> value() const {
        if (filled_ < Window) {
            return std::nullopt;
        }
        std::array<uint16_t, Window> sorted = samples_;
        // Insertion sort: Window is 3 or 5, and pulling in <algorithm> for
        // five elements is not a trade worth making on a microcontroller.
        for (size_t i = 1; i < Window; ++i) {
            const uint16_t key = sorted[i];
            size_t j = i;
            while (j > 0 && sorted[j - 1] > key) {
                sorted[j] = sorted[j - 1];
                --j;
            }
            sorted[j] = key;
        }
        return sorted[Window / 2];
    }

    /// Drop the history.
    ///
    /// Called when the sensor faults or the device wakes: readings from
    /// before a gap describe a room that has had time to change.
    ///
    /// Only the count is cleared. The write position does not need resetting
    /// -- the ring overwrites every slot before `value()` reports again, so
    /// no stale sample can reach a median -- and zeroing it would be a line
    /// no test could distinguish, which is a line that eventually gets a
    /// comment claiming it does something.
    void reset() { filled_ = 0; }

    bool ready() const { return filled_ >= Window; }
    static constexpr size_t window() { return Window; }

   private:
    std::array<uint16_t, Window> samples_{};
    size_t next_ = 0;
    size_t filled_ = 0;
};

}  // namespace nova
