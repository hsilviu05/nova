#include "i2c_bus.hpp"

#include <array>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace nova::hw {

namespace {

constexpr const char *kTag = "i2c";

// Long enough to ride out clock stretching, short enough that a wedged bus
// does not hang the task forever. A jammed peripheral must surface as a
// failed read the caller can report, not as a task that never returns.
constexpr int kTimeoutMs = 100;

i2c_master_bus_handle_t g_bus = nullptr;
SemaphoreHandle_t g_lock = nullptr;

/// RAII guard for the bus mutex.
///
/// Every transaction takes it. Without it, the servo task and the sensor task
/// interleave into a transaction addressed to neither device.
class Guard {
   public:
    Guard() : held_(g_lock != nullptr && xSemaphoreTake(g_lock, portMAX_DELAY) == pdTRUE) {}
    ~Guard() {
        if (held_) {
            xSemaphoreGive(g_lock);
        }
    }
    Guard(const Guard &) = delete;
    Guard &operator=(const Guard &) = delete;

    bool held() const { return held_; }

   private:
    bool held_;
};

}  // namespace

esp_err_t Bus::init() {
    if (g_bus != nullptr) {
        return ESP_OK;
    }

    g_lock = xSemaphoreCreateMutex();
    if (g_lock == nullptr) {
        return ESP_ERR_NO_MEM;
    }

    i2c_master_bus_config_t config = {};
    config.i2c_port = I2C_NUM_0;
    config.sda_io_num = kSdaPin;
    config.scl_io_num = kSclPin;
    config.clk_source = I2C_CLK_SRC_DEFAULT;
    config.glitch_ignore_cnt = 7;
    // The board carries its own pull-ups for the onboard devices. Enabling
    // the internal ones as well puts them in parallel, which weakens the
    // combined pull-up and produces marginal edges at 400 kHz -- a bus that
    // works until a servo draws current and then does not.
    config.flags.enable_internal_pullup = false;

    const esp_err_t err = i2c_new_master_bus(&config, &g_bus);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "bus init failed: %s", esp_err_to_name(err));
        vSemaphoreDelete(g_lock);
        g_lock = nullptr;
        g_bus = nullptr;
    }
    return err;
}

Device Bus::attach(uint8_t address) {
    Device device;
    if (g_bus == nullptr) {
        ESP_LOGE(kTag, "attach 0x%02x before bus init", address);
        return device;
    }

    i2c_device_config_t config = {};
    config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    config.device_address = address;
    config.scl_speed_hz = kBusSpeedHz;

    const esp_err_t err = i2c_master_bus_add_device(g_bus, &config, &device.handle_);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "attach 0x%02x failed: %s", address, esp_err_to_name(err));
        device.handle_ = nullptr;
    }
    return device;
}

bool Bus::present(uint8_t address) {
    if (g_bus == nullptr) {
        return false;
    }
    const Guard guard;
    if (!guard.held()) {
        return false;
    }
    return i2c_master_probe(g_bus, address, kTimeoutMs) == ESP_OK;
}

esp_err_t Device::write(uint8_t reg, const uint8_t *data, size_t length) const {
    if (handle_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    // Register plus payload in one transaction. The PCA9685's auto-increment
    // relies on the four data bytes following the address without a repeated
    // start, so splitting this into two writes would land every byte in the
    // same register.
    if (length > 4) {
        return ESP_ERR_INVALID_SIZE;
    }

    std::array<uint8_t, 5> frame{};
    frame[0] = reg;
    for (size_t i = 0; i < length; ++i) {
        frame[i + 1] = data[i];
    }

    const Guard guard;
    if (!guard.held()) {
        return ESP_ERR_INVALID_STATE;
    }
    return i2c_master_transmit(handle_, frame.data(), length + 1, kTimeoutMs);
}

esp_err_t Device::read(uint8_t reg, uint8_t *out, size_t length) const {
    if (handle_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    const Guard guard;
    if (!guard.held()) {
        return ESP_ERR_INVALID_STATE;
    }
    // Write-then-read with a repeated start, not a stop: a stop between the
    // two lets another task's transaction land in the gap and reset the
    // device's register pointer.
    return i2c_master_transmit_receive(handle_, &reg, 1, out, length, kTimeoutMs);
}

}  // namespace nova::hw
