// The shared I²C bus.
//
// The board reserves only I²C, UART and USB pads for expansion, so every
// off-board peripheral is on one bus: the PCA9685 at 0x40 and the VL53L0X at
// 0x29, alongside the onboard touch controller, IMU and RTC. See
// hardware/BOM.md.
//
// One bus shared by two tasks is a data race waiting to happen -- a servo
// write interleaved with a sensor read produces a transaction that addresses
// neither device. The mutex here is not defensive programming; it is the
// reason the sensor task and the servo task can exist at all.

#pragma once

#include <cstddef>
#include <cstdint>

#include "driver/i2c_master.h"
#include "esp_err.h"

namespace nova::hw {

// GPIO14/15, per the board's expansion header.
inline constexpr gpio_num_t kSdaPin = GPIO_NUM_15;
inline constexpr gpio_num_t kSclPin = GPIO_NUM_14;

// 400 kHz. The PCA9685 and VL53L0X both do 400 kHz; the onboard devices are
// on the same wires, so this is the slowest of what is attached, not the
// fastest either external part can manage.
inline constexpr uint32_t kBusSpeedHz = 400000;

/// One device on the shared bus.
///
/// Every method is serialised against every other device on the same bus, so
/// callers do not need to know that the bus is shared.
class Device {
   public:
    /// Write `length` bytes to `reg`.
    esp_err_t write(uint8_t reg, const uint8_t *data, size_t length) const;

    /// Write a single byte to `reg`.
    esp_err_t write_byte(uint8_t reg, uint8_t value) const { return write(reg, &value, 1); }

    /// Read `length` bytes starting at `reg`.
    esp_err_t read(uint8_t reg, uint8_t *out, size_t length) const;

    /// Read a single byte from `reg`.
    esp_err_t read_byte(uint8_t reg, uint8_t *out) const { return read(reg, out, 1); }

    bool valid() const { return handle_ != nullptr; }

   private:
    friend class Bus;
    i2c_master_dev_handle_t handle_ = nullptr;
};

/// The bus itself. One instance; `init` is idempotent.
class Bus {
   public:
    static esp_err_t init();

    /// Attach a device at `address`.
    ///
    /// Returns an invalid Device if the bus is not up, rather than crashing:
    /// a missing sensor should degrade the product, not stop the boot. NOVA
    /// without a rangefinder still talks, still moves, and still says on the
    /// dashboard that its rangefinder is missing.
    static Device attach(uint8_t address);

    /// Is anything acknowledging at `address`?
    ///
    /// Used at bring-up so a disconnected peripheral is reported as absent
    /// instead of silently producing zeroes for the lifetime of the device.
    static bool present(uint8_t address);
};

}  // namespace nova::hw
