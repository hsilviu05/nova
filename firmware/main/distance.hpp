// The VL53L0X, in single-shot mode.
//
// **Scope, stated plainly.** This uses the sensor's power-on defaults. It
// does not run ST's tuning register set, and it does not perform SPAD or
// reference-SPAD calibration. Those live in ST's driver as roughly two
// hundred writes whose values are not derivable from the datasheet, and
// inventing them would put guesswork underneath the signal the whole product
// depends on.
//
// What that costs: absolute accuracy degrades from roughly ±3% to something
// nearer ±10%, and ambient-light rejection is worse. What NOVA actually needs
// from this part is "is there a person within about 70 cm of the desk", with
// hysteresis of 30 cm around that decision and an 800 ms debounce on top. A
// ten-percent error does not reach those thresholds.
//
// If the sensor later needs to be accurate rather than merely reliable, the
// fix is to add ST's driver as a managed component and call its init here.
// The interpretation and filtering in core/rangefinder.hpp do not change.
//
// The register addresses below are the ones common to every open driver for
// this part. They should be confirmed against real hardware at bring-up --
// `model_id_ok()` exists for exactly that.

#pragma once

#include <cstdint>
#include <optional>

#include "esp_err.h"
#include "i2c_bus.hpp"
#include "nova/rangefinder.hpp"

namespace nova::hw {

inline constexpr uint8_t kRangefinderAddress = 0x29;

// Registers.
inline constexpr uint8_t kSysrangeStart = 0x00;
inline constexpr uint8_t kSystemInterruptClear = 0x0B;
inline constexpr uint8_t kResultInterruptStatus = 0x13;
// RESULT_RANGE_STATUS begins a 12-byte block; the range sits at offset 10.
inline constexpr uint8_t kResultBlock = 0x14;
inline constexpr size_t kResultBlockBytes = 12;
inline constexpr size_t kRangeOffset = 10;

/// Give up on a measurement after this long.
///
/// A single-shot measurement completes in about 30 ms at the default timing
/// budget. Polling forever would let one wedged sensor stop the main loop and
/// with it the face, the head, and the heartbeat -- a broken sensor must cost
/// the product a sensor, not the product.
inline constexpr uint32_t kMeasurementTimeoutMs = 100;

class Rangefinder {
   public:
    esp_err_t begin();

    /// Take one measurement and fold it into the filter.
    ///
    /// Returns the filtered distance in centimetres once the filter's window
    /// has filled, or nothing while it is still settling, when the reading
    /// failed, or when there is nothing within range. That `nothing` maps
    /// directly onto `Observation::distance_cm`, which is why the behaviour
    /// engine has never needed to know a sensor exists.
    std::optional<int> read();

    bool available() const { return device_.valid() && present_; }

    /// Consecutive failed reads. Reported in telemetry: a sensor degrading
    /// over weeks is something the dashboard should show before it becomes a
    /// robot that has stopped noticing anyone.
    uint32_t consecutive_failures() const { return failures_; }

   private:
    esp_err_t start_measurement();
    std::optional<RangeReading> collect();

    Device device_;
    RangeFilter<5> filter_;
    bool present_ = false;
    uint32_t failures_ = 0;
};

}  // namespace nova::hw
