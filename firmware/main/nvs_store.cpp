#include "nvs_store.hpp"

#include <vector>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

namespace nova::hw {

namespace {

constexpr const char *kTag = "store";
constexpr const char *kNamespace = "nova";

constexpr const char *kKeySsid = "ssid";
constexpr const char *kKeyPassword = "pass";
constexpr const char *kKeyToken = "token";
constexpr const char *kKeyDeviceId = "devid";

/// Read one string. Returns empty if absent, unreadable, or longer than the
/// cap -- an oversized value means something wrote garbage, and treating it
/// as absent sends the device back to provisioning rather than forward with a
/// corrupt credential.
std::string read_string(const char *key, size_t cap) {
    nvs_handle_t handle = 0;
    if (nvs_open(kNamespace, NVS_READONLY, &handle) != ESP_OK) {
        return {};
    }

    size_t length = 0;
    esp_err_t err = nvs_get_str(handle, key, nullptr, &length);
    if (err != ESP_OK || length == 0 || length > cap + 1) {
        nvs_close(handle);
        return {};
    }

    std::vector<char> buffer(length);
    err = nvs_get_str(handle, key, buffer.data(), &length);
    nvs_close(handle);
    if (err != ESP_OK) {
        return {};
    }
    // length includes the terminator.
    return std::string(buffer.data(), length - 1);
}

esp_err_t write_string(const char *key, const std::string &value) {
    nvs_handle_t handle = 0;
    esp_err_t err = nvs_open(kNamespace, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(handle, key, value.c_str());
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    return err;
}

}  // namespace

esp_err_t Store::init() {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(kTag, "NVS unusable, reformatting -- credentials will be lost");
        const esp_err_t erased = nvs_flash_erase();
        if (erased != ESP_OK) {
            return erased;
        }
        err = nvs_flash_init();
    }
    return err;
}

WifiCredentials Store::wifi() {
    WifiCredentials credentials;
    credentials.ssid = read_string(kKeySsid, kMaxSsidLength);
    credentials.password = read_string(kKeyPassword, kMaxPasswordLength);
    return credentials;
}

esp_err_t Store::save_wifi(const WifiCredentials &credentials) {
    if (credentials.ssid.empty() || credentials.ssid.size() > kMaxSsidLength ||
        credentials.password.size() > kMaxPasswordLength) {
        return ESP_ERR_INVALID_ARG;
    }
    const esp_err_t err = write_string(kKeySsid, credentials.ssid);
    if (err != ESP_OK) {
        return err;
    }
    // Deliberately not logged, at any level. See the header.
    return write_string(kKeyPassword, credentials.password);
}

std::string Store::token() { return read_string(kKeyToken, kMaxTokenLength); }

esp_err_t Store::save_token(const std::string &token) {
    if (token.empty() || token.size() > kMaxTokenLength) {
        return ESP_ERR_INVALID_ARG;
    }
    return write_string(kKeyToken, token);
}

std::string Store::device_id() { return read_string(kKeyDeviceId, 64); }

esp_err_t Store::save_device_id(const std::string &device_id) {
    if (device_id.empty() || device_id.size() > 64) {
        return ESP_ERR_INVALID_ARG;
    }
    return write_string(kKeyDeviceId, device_id);
}

esp_err_t Store::forget() {
    nvs_handle_t handle = 0;
    esp_err_t err = nvs_open(kNamespace, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    // Erase the whole namespace rather than the keys we happen to remember:
    // a key added later and forgotten here would survive an unpair, which is
    // precisely the kind of omission that leaks a credential to the next
    // owner of the device.
    err = nvs_erase_all(handle);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    ESP_LOGI(kTag, "credentials erased");
    return err;
}

}  // namespace nova::hw
