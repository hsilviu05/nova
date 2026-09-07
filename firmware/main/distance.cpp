#include "distance.hpp"

#include <array>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace nova::hw {

namespace {

constexpr const char *kTag = "range";

/// Failures before the filter's history is thrown away.
///
/// A few dropped readings are ordinary. A run of them means the sensor lost
/// sight of the room for long enough that what it saw before is no longer a
/// description of it, and continuing to median over pre-gap samples would
/// report a person who left during the gap.
constexpr uint32_t kFailuresBeforeReset = 5;

uint32_t now_ms() {
    return static_cast<uint32_t>(esp_timer_get_time() / 1000);
}

}  // namespace

esp_err_t Rangefinder::begin() {
    if (!Bus::present(kRangefinderAddress)) {
        ESP_LOGE(kTag, "no VL53L0X at 0x%02x", kRangefinderAddress);
        return ESP_ERR_NOT_FOUND;
    }

    device_ = Bus::attach(kRangefinderAddress);
    if (!device_.valid()) {
        return ESP_ERR_NOT_FOUND;
    }

    // An empty bus reads as 0x00 or 0xFF rather than failing, so without this
    // check a missing sensor is indistinguishable from a sensor that never
    // sees anybody -- which is the hardest possible bug to notice, because
    // "the robot never reacts" looks like a behaviour problem.
    uint8_t model_id = 0;
    const esp_err_t err = device_.read_byte(kModelIdRegister, &model_id);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "model id read failed: %s", esp_err_to_name(err));
        return err;
    }
    if (model_id != kModelId) {
        ESP_LOGE(kTag, "model id 0x%02x, expected 0x%02x -- wrong part or a bad bus",
                 model_id, kModelId);
        return ESP_ERR_INVALID_RESPONSE;
    }

    present_ = true;
    ESP_LOGI(kTag, "VL53L0X ready (factory defaults, uncalibrated)");
    return ESP_OK;
}

esp_err_t Rangefinder::start_measurement() {
    return device_.write_byte(kSysrangeStart, 0x01);
}

std::optional<RangeReading> Rangefinder::collect() {
    const uint32_t deadline = now_ms() + kMeasurementTimeoutMs;

    while (true) {
        uint8_t interrupt_status = 0;
        if (device_.read_byte(kResultInterruptStatus, &interrupt_status) != ESP_OK) {
            return std::nullopt;
        }
        if ((interrupt_status & 0x07) != 0) {
            break;
        }
        // Signed comparison of the difference, so the 49-day rollover of the
        // millisecond counter does not turn this into an infinite loop.
        if (static_cast<int32_t>(now_ms() - deadline) >= 0) {
            ESP_LOGW(kTag, "measurement timed out");
            return std::nullopt;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    std::array<uint8_t, kResultBlockBytes> block{};
    if (device_.read(kResultBlock, block.data(), block.size()) != ESP_OK) {
        return std::nullopt;
    }

    // The interrupt must be cleared or the next measurement reports ready
    // immediately and returns this same reading forever -- a sensor that
    // appears to work and has in fact frozen.
    if (device_.write_byte(kSystemInterruptClear, 0x01) != ESP_OK) {
        return std::nullopt;
    }

    const uint16_t millimetres =
        static_cast<uint16_t>((block[kRangeOffset] << 8) | block[kRangeOffset + 1]);
    return interpret(block[0], millimetres);
}

std::optional<int> Rangefinder::read() {
    if (!available()) {
        return std::nullopt;
    }

    if (start_measurement() != ESP_OK) {
        ++failures_;
        return std::nullopt;
    }

    const std::optional<RangeReading> reading = collect();
    if (!reading.has_value()) {
        ++failures_;
        if (failures_ == kFailuresBeforeReset) {
            filter_.reset();
        }
        return std::nullopt;
    }

    failures_ = 0;

    if (reading->quality == RangeQuality::OutOfRange) {
        // An empty desk is a real observation, not a fault. It must not be
        // pushed into the filter as a distance, and it must not reset it
        // either: someone who steps away for one sample has not made the
        // previous four meaningless.
        return std::nullopt;
    }
    if (!reading->usable()) {
        return std::nullopt;
    }

    filter_.push(reading->millimetres);
    const std::optional<uint16_t> filtered = filter_.value();
    if (!filtered.has_value()) {
        return std::nullopt;
    }
    return to_centimetres(*filtered);
}

}  // namespace nova::hw
