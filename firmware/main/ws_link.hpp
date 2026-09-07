// The link to the backend.
//
// Endpoint, close codes and frame shapes all mirror
// services/api/src/nova/api/v1/device_ws.py. The encoding and decoding are
// core/protocol's job; this file moves bytes and nothing else.
//
// The token goes in the query string rather than an Authorization header:
// the ESP-IDF WebSocket client cannot reliably attach custom headers to the
// upgrade request, and the server accepts both for that reason. It is inside
// TLS either way, and it is logged nowhere on either side.

#pragma once

#include <cstdint>
#include <functional>
#include <string>

#include "esp_err.h"
#include "esp_websocket_client.h"
#include "nova/connection.hpp"
#include "nova/protocol.hpp"

namespace nova::hw {

inline constexpr const char *kDeviceSocketPath = "/api/v1/devices/ws";

// Application close codes from the server. 4001 says the credential is bad
// and retrying will not fix it; anything else is a network event.
inline constexpr int kCloseUnauthorised = 4001;
inline constexpr int kCloseProtocolViolation = 4003;

class Link {
   public:
    /// Called with each inbound frame, already decoded. Runs on the WebSocket
    /// client's event task, so it must not block -- it queues, it does not act.
    using FrameHandler = std::function<void(const Inbound &)>;

    esp_err_t begin(const std::string &base_url, const std::string &token, FrameHandler handler);

    /// Open the connection. Non-blocking; `connected()` reports the outcome.
    esp_err_t open();
    void close();

    /// Send one already-encoded frame.
    esp_err_t send(const std::string &json);

    bool connected() const { return connected_; }

    /// Why the last attempt ended, for ConnectionPolicy.
    Failure last_failure() const { return last_failure_; }

    /// Milliseconds since anything arrived. TCP does not notice a router that
    /// vanished, so this is what detects a socket that is open and dead.
    uint32_t silent_for(uint32_t now_ms) const { return now_ms - last_frame_ms_; }

   private:
    static void on_event(void *handler_args, esp_event_base_t base, int32_t id, void *data);
    void handle(int32_t id, const esp_websocket_event_data_t *event);

    esp_websocket_client_handle_t client_ = nullptr;
    FrameHandler handler_;
    std::string url_;
    bool connected_ = false;
    Failure last_failure_ = Failure::Transient;
    uint32_t last_frame_ms_ = 0;
};

}  // namespace nova::hw
