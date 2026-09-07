#include "servos.hpp"

#include "esp_log.h"
#include "esp_rom_sys.h"
#include "nova/pca9685.hpp"

namespace nova::hw {

namespace {

constexpr const char *kTag = "servo";

/// Replay one encoded sequence onto the bus.
///
/// The delays are part of the sequence, not the caller's responsibility --
/// which is the point of encoding them as steps. `esp_rom_delay_us` rather
/// than `vTaskDelay`: the oscillator settle is 500 µs and a FreeRTOS tick is
/// 1 ms, so a task delay would round it to either nothing or twice as long.
template <size_t Capacity>
esp_err_t replay(const Device &device, const pca9685::Sequence<Capacity> &sequence) {
    for (size_t i = 0; i < sequence.size(); ++i) {
        const pca9685::Step &step = sequence[i];
        if (step.kind == pca9685::Step::Kind::Delay) {
            esp_rom_delay_us(step.delay_us);
            continue;
        }
        const esp_err_t err = device.write(step.reg, step.data.data(), step.length);
        if (err != ESP_OK) {
            return err;
        }
    }
    return ESP_OK;
}

}  // namespace

esp_err_t Head::begin() {
    if (!Bus::present(pca9685::kDefaultAddress)) {
        ESP_LOGE(kTag, "no PCA9685 at 0x%02x", pca9685::kDefaultAddress);
        return ESP_ERR_NOT_FOUND;
    }

    device_ = Bus::attach(pca9685::kDefaultAddress);
    if (!device_.valid()) {
        return ESP_ERR_NOT_FOUND;
    }

    const esp_err_t err = replay(device_, pca9685::configure(50));
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "configure failed: %s", esp_err_to_name(err));
        return err;
    }
    configured_ = true;

    // Centre before anything else commands a pose, so the first real movement
    // is a known short travel rather than a lunge from wherever the servo
    // happened to be parked.
    return move_to(HeadPose::clamped(0, 0), 0);
}

esp_err_t Head::apply_angle(uint8_t channel, int degrees) {
    return replay(device_, pca9685::set_angle(channel, degrees));
}

esp_err_t Head::move_to(const HeadPose &target, uint32_t now_ms) {
    if (!available()) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = apply_angle(kYawChannel, target.yaw);
    if (err == ESP_OK) {
        err = apply_angle(kPitchChannel, target.pitch);
    }
    if (err != ESP_OK) {
        ESP_LOGW(kTag, "move failed: %s", esp_err_to_name(err));
        return err;
    }

    pose_ = target;
    driving_ = true;
    last_move_ms_ = now_ms;
    return ESP_OK;
}

void Head::tick(uint32_t now_ms) {
    if (!driving_ || !available()) {
        return;
    }
    // Unsigned subtraction wraps correctly across the 49-day rollover of a
    // millisecond counter, so this stays right on a device left running.
    if (now_ms - last_move_ms_ < kIdleReleaseMs) {
        return;
    }
    relax();
}

esp_err_t Head::relax() {
    if (!available()) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = replay(device_, pca9685::release(kYawChannel));
    if (err == ESP_OK) {
        err = replay(device_, pca9685::release(kPitchChannel));
    }
    if (err == ESP_OK) {
        driving_ = false;
    }
    return err;
}

}  // namespace nova::hw
