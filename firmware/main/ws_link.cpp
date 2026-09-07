#include "ws_link.hpp"

#include <cstring>

#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"

namespace nova::hw {

namespace {

constexpr const char *kTag = "link";

uint32_t now_ms() {
    return static_cast<uint32_t>(esp_timer_get_time() / 1000);
}

}  // namespace

esp_err_t Link::begin(const std::string &base_url, const std::string &token,
                      FrameHandler handler) {
    if (base_url.empty() || token.empty()) {
        return ESP_ERR_INVALID_ARG;
    }

    handler_ = std::move(handler);
    url_ = base_url + kDeviceSocketPath + "?token=" + token;

    esp_websocket_client_config_t config = {};
    config.uri = url_.c_str();
    // Frames larger than the protocol's own cap are a bug or an attack; the
    // server rejects them before parsing and the device should not allocate
    // for them either.
    config.buffer_size = static_cast<int>(kMaxFrameBytes);
    // The client's own reconnect is off. ConnectionPolicy owns retry timing,
    // and two independent backoffs would fight: the driver would reconnect
    // during the policy's wait, and neither would be the behaviour that was
    // tested.
    config.disable_auto_reconnect = true;
    config.network_timeout_ms = 10000;
    // Certificate verification uses the bundle compiled into the image. This
    // is the whole security of the link: without it the token, the telemetry
    // and every command are available to anything on the network path.
    config.crt_bundle_attach = esp_crt_bundle_attach;

    client_ = esp_websocket_client_init(&config);
    if (client_ == nullptr) {
        // The URL contains the token, so the failure is reported without it.
        ESP_LOGE(kTag, "client init failed");
        return ESP_FAIL;
    }

    return esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY, &Link::on_event, this);
}

esp_err_t Link::open() {
    if (client_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    last_failure_ = Failure::Transient;
    last_frame_ms_ = now_ms();
    return esp_websocket_client_start(client_);
}

void Link::close() {
    if (client_ == nullptr) {
        return;
    }
    esp_websocket_client_close(client_, portMAX_DELAY);
    connected_ = false;
}

esp_err_t Link::send(const std::string &json) {
    if (client_ == nullptr || !connected_) {
        return ESP_ERR_INVALID_STATE;
    }
    if (json.size() > kMaxFrameBytes) {
        return ESP_ERR_INVALID_SIZE;
    }

    const int sent = esp_websocket_client_send_text(client_, json.c_str(),
                                                    static_cast<int>(json.size()),
                                                    portMAX_DELAY);
    return sent < 0 ? ESP_FAIL : ESP_OK;
}

void Link::on_event(void *handler_args, esp_event_base_t, int32_t id, void *data) {
    auto *self = static_cast<Link *>(handler_args);
    if (self != nullptr) {
        self->handle(id, static_cast<esp_websocket_event_data_t *>(data));
    }
}

void Link::handle(int32_t id, const esp_websocket_event_data_t *event) {
    switch (id) {
        case WEBSOCKET_EVENT_CONNECTED:
            connected_ = true;
            last_frame_ms_ = now_ms();
            ESP_LOGI(kTag, "connected");
            return;

        case WEBSOCKET_EVENT_DISCONNECTED:
            connected_ = false;
            ESP_LOGW(kTag, "disconnected");
            return;

        case WEBSOCKET_EVENT_CLOSED:
            connected_ = false;
            // The close *code* does not arrive here -- it comes through as a
            // close frame in the DATA event below, and last_failure_ is set
            // there. This event only says the socket is gone.
            ESP_LOGW(kTag, "closed");
            return;

        case WEBSOCKET_EVENT_ERROR:
            connected_ = false;
            ESP_LOGW(kTag, "transport error");
            return;

        case WEBSOCKET_EVENT_DATA: {
            if (event == nullptr || event->data_ptr == nullptr || event->data_len <= 0) {
                return;
            }
            last_frame_ms_ = now_ms();

            // A close frame carries the reason in its first two bytes, big-
            // endian. This is where 4001 is read, and reading it is what
            // separates "your token is bad" from "the wifi went away" -- with
            // the two conflated the device retries a revoked credential every
            // minute forever and never tells its owner why.
            if (event->op_code == 0x08) {
                if (event->data_len >= 2) {
                    const int code = (static_cast<unsigned char>(event->data_ptr[0]) << 8) |
                                     static_cast<unsigned char>(event->data_ptr[1]);
                    if (code == kCloseUnauthorised) {
                        last_failure_ = Failure::Rejected;
                        ESP_LOGE(kTag, "credential refused by the server");
                    } else if (code == kCloseProtocolViolation) {
                        // Not a credential problem, and not transient either:
                        // it means this firmware sent something the server
                        // would not accept. Retrying is right -- the bug is
                        // ours to find in the logs, not the device's to solve
                        // by giving up on its owner.
                        ESP_LOGE(kTag, "server rejected a frame we sent (4003)");
                    }
                }
                return;
            }

            // 0x1 is text. 0x9/0xA are ping and pong, handled by the client.
            if (event->op_code != 0x01) {
                return;
            }
            // A fragmented message arrives in pieces. Decoding a piece would
            // produce an Invalid frame and an error sent back to the server
            // for a message that was fine, so partial payloads are skipped:
            // protocol frames are small and the server does not fragment them.
            if (event->payload_offset != 0 || event->data_len != event->payload_len) {
                ESP_LOGW(kTag, "fragmented frame ignored (%d of %d bytes)", event->data_len,
                         event->payload_len);
                return;
            }

            const std::string json(event->data_ptr, static_cast<size_t>(event->data_len));
            const Inbound frame = decode(json);
            if (handler_) {
                handler_(frame);
            }
            return;
        }

        default:
            return;
    }
}

}  // namespace nova::hw
