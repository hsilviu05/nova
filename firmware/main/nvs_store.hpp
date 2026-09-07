// Persistent configuration: wifi credentials and the device token.
//
// Two rules govern everything in this file.
//
// **Nothing here is ever logged.** Not at debug level, not "temporarily", not
// the first four characters. UART logs get pasted into issue reports and
// screen-shared during demos, and a device token is a credential that speaks
// for the physical object on somebody's desk. The accessors return values;
// they never print them.
//
// **Erasing must actually erase.** `forget()` is what a user gets when they
// give the device away or return it, so it removes the wifi password and the
// token, not just a flag saying to ignore them.
//
// NVS on the ESP32 is not encrypted unless flash encryption is enabled in
// hardware. Enabling it is a production step recorded in the firmware README,
// not something this file can do on its own -- so the honest security
// property today is "an attacker with physical access and a flash reader can
// read these", and the mitigation is that the token is revocable from the
// dashboard.

#pragma once

#include <cstddef>
#include <string>

#include "esp_err.h"

namespace nova::hw {

/// Longest values NVS will be asked to hold. WPA2 caps a passphrase at 63
/// characters; the token is a server-issued opaque string.
inline constexpr size_t kMaxSsidLength = 32;
inline constexpr size_t kMaxPasswordLength = 64;
inline constexpr size_t kMaxTokenLength = 256;

struct WifiCredentials {
    std::string ssid;
    std::string password;

    bool complete() const { return !ssid.empty(); }
};

class Store {
   public:
    /// Initialise NVS, reformatting if the partition is unusable.
    ///
    /// A truncated OTA or a version bump can leave NVS in a state that will
    /// not mount. Reformatting loses the credentials, which means the device
    /// returns to provisioning -- recoverable by the owner in a minute, and
    /// far better than a device that refuses to boot.
    static esp_err_t init();

    static WifiCredentials wifi();
    static esp_err_t save_wifi(const WifiCredentials &credentials);

    /// The device token, or empty if the device has not been claimed.
    static std::string token();
    static esp_err_t save_token(const std::string &token);

    /// The device's stable identifier, assigned by the server at claim time.
    static std::string device_id();
    static esp_err_t save_device_id(const std::string &device_id);

    /// Remove every credential. Used when the owner unpairs the device, and
    /// when the server rejects our token often enough that it is not coming
    /// back (see core/connection.hpp).
    static esp_err_t forget();
};

}  // namespace nova::hw
