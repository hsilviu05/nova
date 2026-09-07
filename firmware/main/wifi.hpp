// WiFi, in station mode.
//
// Reconnection timing is not decided here -- `nova::ConnectionPolicy` in
// core/ owns that, and is tested. This file connects, reports what happened,
// and gets out of the way.
//
// One thing it deliberately does not do is store credentials in the ESP-IDF
// wifi driver's own NVS namespace. `WIFI_STORAGE_RAM` keeps them out of a
// second copy on flash that `Store::forget()` would not clear -- an unpair
// that leaves the password behind in another namespace is not an unpair.

#pragma once

#include <cstdint>

#include "esp_err.h"
#include "nvs_store.hpp"

namespace nova::hw {

/// How long to wait for an association and a DHCP lease.
///
/// Generous: a congested 2.4 GHz band on a cheap router can take this long
/// legitimately, and giving up early turns a slow network into a device that
/// never connects at all.
inline constexpr uint32_t kWifiConnectTimeoutMs = 20000;

class Wifi {
   public:
    /// Bring up the driver. Does not connect.
    static esp_err_t init();

    /// Associate and wait for an IP address.
    ///
    /// Blocks for at most kWifiConnectTimeoutMs. Distinguishes a refused
    /// password from an absent network, because the two need different things
    /// from the owner and a device that says only "cannot connect" makes them
    /// guess.
    static esp_err_t connect(const WifiCredentials &credentials);

    static void disconnect();
    static bool connected();

    /// Signal strength in dBm, or 0 if not associated. Sent as telemetry, and
    /// it is genuinely diagnostic: a device at the edge of range disconnects
    /// in ways that look like a backend fault from every other angle.
    static int rssi();
};

}  // namespace nova::hw
