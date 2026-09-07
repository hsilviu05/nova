#include "wifi.hpp"

#include <cstring>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

namespace nova::hw {

namespace {

constexpr const char *kTag = "wifi";

constexpr EventBits_t kConnectedBit = BIT0;
constexpr EventBits_t kFailedBit = BIT1;

EventGroupHandle_t g_events = nullptr;
bool g_connected = false;
uint8_t g_last_disconnect_reason = 0;

void on_wifi_event(void *, esp_event_base_t base, int32_t id, void *data) {
    if (base != WIFI_EVENT) {
        return;
    }
    if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const auto *event = static_cast<wifi_event_sta_disconnected_t *>(data);
        g_last_disconnect_reason = event->reason;
        g_connected = false;
        // No automatic reconnect here. Retry timing belongs to
        // ConnectionPolicy, and a driver-level retry racing that policy
        // produces two independent backoffs fighting each other.
        xEventGroupSetBits(g_events, kFailedBit);
    }
}

void on_ip_event(void *, esp_event_base_t base, int32_t id, void *) {
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        g_connected = true;
        xEventGroupSetBits(g_events, kConnectedBit);
    }
}

/// Is this disconnect reason a refused password rather than a missing
/// network? The owner needs to know which; "cannot connect" makes them guess.
bool is_auth_failure(uint8_t reason) {
    switch (reason) {
        case WIFI_REASON_AUTH_EXPIRE:
        case WIFI_REASON_AUTH_FAIL:
        case WIFI_REASON_HANDSHAKE_TIMEOUT:
        case WIFI_REASON_MIC_FAILURE:
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
            return true;
        default:
            return false;
    }
}

}  // namespace

esp_err_t Wifi::init() {
    if (g_events != nullptr) {
        return ESP_OK;
    }

    g_events = xEventGroupCreate();
    if (g_events == nullptr) {
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&config));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &on_wifi_event, nullptr, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &on_ip_event, nullptr, nullptr));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    // Credentials live in one place, ours, so that forget() is complete.
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    return esp_wifi_start();
}

esp_err_t Wifi::connect(const WifiCredentials &credentials) {
    if (!credentials.complete()) {
        return ESP_ERR_INVALID_ARG;
    }

    wifi_config_t config = {};
    std::strncpy(reinterpret_cast<char *>(config.sta.ssid), credentials.ssid.c_str(),
                 sizeof(config.sta.ssid) - 1);
    std::strncpy(reinterpret_cast<char *>(config.sta.password), credentials.password.c_str(),
                 sizeof(config.sta.password) - 1);

    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &config);
    if (err != ESP_OK) {
        return err;
    }

    xEventGroupClearBits(g_events, kConnectedBit | kFailedBit);
    g_last_disconnect_reason = 0;

    err = esp_wifi_connect();
    if (err != ESP_OK) {
        return err;
    }

    const EventBits_t bits =
        xEventGroupWaitBits(g_events, kConnectedBit | kFailedBit, pdFALSE, pdFALSE,
                            pdMS_TO_TICKS(kWifiConnectTimeoutMs));

    if ((bits & kConnectedBit) != 0) {
        ESP_LOGI(kTag, "connected, rssi %d dBm", rssi());
        return ESP_OK;
    }
    if ((bits & kFailedBit) != 0) {
        if (is_auth_failure(g_last_disconnect_reason)) {
            ESP_LOGE(kTag, "authentication refused (reason %u)", g_last_disconnect_reason);
            return ESP_ERR_WIFI_PASSWORD;
        }
        ESP_LOGW(kTag, "disconnected (reason %u)", g_last_disconnect_reason);
        return ESP_ERR_WIFI_NOT_CONNECT;
    }
    ESP_LOGW(kTag, "connect timed out");
    return ESP_ERR_TIMEOUT;
}

void Wifi::disconnect() {
    esp_wifi_disconnect();
    g_connected = false;
}

bool Wifi::connected() { return g_connected; }

int Wifi::rssi() {
    if (!g_connected) {
        return 0;
    }
    wifi_ap_record_t record = {};
    if (esp_wifi_sta_get_ap_info(&record) != ESP_OK) {
        return 0;
    }
    return record.rssi;
}

}  // namespace nova::hw
