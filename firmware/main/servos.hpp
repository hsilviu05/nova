// The head, as two servos on a PCA9685.
//
// This file contains no decisions. It replays the command sequences that
// core/pca9685 encodes onto the bus, and it stops driving the servos when
// nobody is asking them to move. Everything about *which* angle is correct
// lives in core/geometry and core/behaviour, where it is tested.

#pragma once

#include <cstdint>

#include "esp_err.h"
#include "i2c_bus.hpp"
#include "nova/geometry.hpp"

namespace nova::hw {

// Channel assignment on the PCA9685 board.
inline constexpr uint8_t kYawChannel = 0;
inline constexpr uint8_t kPitchChannel = 1;

/// How long the head can sit still before its servos are released.
///
/// A servo commanded to a position holds it under load, drawing current and
/// emitting a fine hum. On a desk at night that hum is the single most
/// annoying property the whole device could have, so a head that has not been
/// asked to move for a few seconds goes limp. It is geared enough to stay put.
inline constexpr uint32_t kIdleReleaseMs = 4000;

class Head {
   public:
    /// Attach and run the PCA9685 bring-up sequence.
    ///
    /// Fails loudly if the chip does not answer. A head that silently does
    /// not move is diagnosed by taking the case apart; one that reports a
    /// missing PCA9685 at boot is diagnosed from the dashboard.
    esp_err_t begin();

    /// Move to a pose. Clamped by construction -- HeadPose cannot hold an
    /// angle the mechanism cannot reach.
    esp_err_t move_to(const HeadPose &pose, uint32_t now_ms);

    /// Release both servos if the head has been idle long enough.
    ///
    /// Called from the main loop rather than a timer so that "idle" is
    /// measured against the same clock the behaviour engine uses.
    void tick(uint32_t now_ms);

    /// Stop driving both servos now. Used when the device sleeps.
    esp_err_t relax();

    bool available() const { return device_.valid() && configured_; }
    HeadPose pose() const { return pose_; }

   private:
    esp_err_t apply_angle(uint8_t channel, int degrees);

    Device device_;
    HeadPose pose_{};
    bool configured_ = false;
    bool driving_ = false;
    uint32_t last_move_ms_ = 0;
};

}  // namespace nova::hw
