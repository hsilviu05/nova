// Entry point and wiring.
//
// This file has no opinions. It reads sensors, hands an Observation to the
// behaviour engine, performs the Action that comes back, and moves frames
// between the outbox and the socket. Every decision it appears to make is
// made in core/, where there is a test for it.
//
// That is the arrangement the whole architecture rests on:
//
//   The language model never reaches GPIO. It emits an intent; the behaviour
//   engine decides what that means for a servo. Nothing upstream of the
//   engine can address a pin, and nothing downstream of it makes a decision.
//
// **This file has never been compiled.** The Xtensa toolchain is not
// installed in this repository's CI, and no board exists yet -- see
// firmware/README.md. Everything it calls into is host-tested; the wiring
// itself is not, which is exactly why there is as little of it as possible.

#include <cstdio>
#include <memory>
#include <new>
#include <string>

#include "distance.hpp"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "i2c_bus.hpp"
#include "nova/behaviour.hpp"
#include "nova/connection.hpp"
#include "nova/outbox.hpp"
#include "nova/protocol.hpp"
#include "nvs_store.hpp"
#include "servos.hpp"
#include "wifi.hpp"
#include "ws_link.hpp"

namespace {

constexpr const char *kTag = "nova";

// The loop sleeps 50 ms per pass, and a single-shot rangefinder measurement
// takes roughly 30 ms of that pass, so the real rate is nearer 12 Hz than
// 20. That is still ten samples inside the 800 ms presence debounce, which
// is what the number has to buy; the rest of the time the CPU is idle, which
// is most of the battery.
constexpr uint32_t kTickMs = 50;

constexpr uint32_t kHeartbeatIntervalMs = 30000;

// Inbound frames are queued from the socket's event task and handled in the
// main loop, so a command never runs concurrently with a behaviour update.
constexpr size_t kInboundQueueDepth = 8;

// Compiled in rather than provisioned: pointing a device at a different
// backend is a firmware change, not a runtime one, and a runtime-settable
// server URL is a way to redirect somebody's device to an attacker.
constexpr const char *kBackendUrl = CONFIG_NOVA_BACKEND_URL;

QueueHandle_t g_inbound = nullptr;

uint32_t now_ms() {
    return static_cast<uint32_t>(esp_timer_get_time() / 1000);
}

uint32_t uptime_seconds() {
    return static_cast<uint32_t>(esp_timer_get_time() / 1000000);
}

/// A unit value in [0, 1) from the hardware RNG, for the reconnect jitter.
double random_unit() {
    return static_cast<double>(esp_random()) / 4294967296.0;
}

/// A version-4 UUID for a frame id.
///
/// The server treats these as opaque, but they are the only way a command
/// result can be matched to its command, so they must not collide. The
/// hardware RNG is seeded from thermal noise and is not the pseudo-random
/// generator that would give every device on a shelf the same first id.
std::string frame_id() {
    uint8_t bytes[16];
    for (auto &byte : bytes) {
        byte = static_cast<uint8_t>(esp_random() & 0xFF);
    }
    bytes[6] = static_cast<uint8_t>((bytes[6] & 0x0F) | 0x40);
    bytes[8] = static_cast<uint8_t>((bytes[8] & 0x3F) | 0x80);

    char text[37];
    std::snprintf(text, sizeof(text),
                  "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                  bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6],
                  bytes[7], bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13],
                  bytes[14], bytes[15]);
    return std::string(text);
}

const char *state_name(nova::State state) {
    switch (state) {
        case nova::State::Idle: return "idle";
        case nova::State::Listening: return "listening";
        case nova::State::Thinking: return "thinking";
        case nova::State::Speaking: return "speaking";
        case nova::State::Curious: return "curious";
        case nova::State::Happy: return "happy";
        case nova::State::Confused: return "confused";
        case nova::State::Alert: return "alert";
        case nova::State::Sleeping: return "sleeping";
        case nova::State::Offline: return "offline";
    }
    return "idle";
}

/// RFC 3339 from the device clock.
///
/// Until an RTC sync lands, this is uptime rather than wall time, and it is
/// marked as such rather than fabricated: a plausible-looking wrong timestamp
/// would corrupt every habit the analytics phase infers, and a timestamp the
/// server can recognise as unsynced can be corrected on arrival.
std::string device_timestamp() {
    const uint32_t seconds = uptime_seconds();
    char text[40];
    std::snprintf(text, sizeof(text), "1970-01-01T%02u:%02u:%02uZ",
                  (seconds / 3600) % 24, (seconds / 60) % 60, seconds % 60);
    return std::string(text);
}

struct Peripherals {
    nova::hw::Head head;
    nova::hw::Rangefinder rangefinder;
};

/// Bring up the hardware, reporting what is missing rather than refusing to
/// run without it. A NOVA with no rangefinder still talks and still moves;
/// one that halts at boot because a sensor is unplugged helps nobody.
void start_peripherals(Peripherals &peripherals) {
    if (nova::hw::Bus::init() != ESP_OK) {
        ESP_LOGE(kTag, "I2C bus unavailable: no head, no rangefinder");
        return;
    }
    if (peripherals.head.begin() != ESP_OK) {
        ESP_LOGW(kTag, "head unavailable");
    }
    if (peripherals.rangefinder.begin() != ESP_OK) {
        ESP_LOGW(kTag, "rangefinder unavailable: presence detection is off");
    }
}

/// Queue an inbound frame for the main loop.
///
/// Runs on the socket's event task, so it must not block and must not touch
/// the behaviour engine. A full queue drops the frame and says so: silently
/// discarding a command would leave the server believing it was obeyed.
///
/// The queue carries pointers, not frames. `Inbound` holds std::strings, and
/// a FreeRTOS queue copies raw bytes -- which would give two objects the same
/// heap buffer and free it twice. Ownership passes to the consumer.
void on_frame(const nova::Inbound &frame) {
    if (g_inbound == nullptr) {
        return;
    }
    auto *copy = new (std::nothrow) nova::Inbound(frame);
    if (copy == nullptr) {
        ESP_LOGW(kTag, "out of memory, inbound frame dropped");
        return;
    }
    if (xQueueSend(g_inbound, &copy, 0) != pdTRUE) {
        ESP_LOGW(kTag, "inbound queue full, frame dropped");
        delete copy;
    }
}

void run(Peripherals &peripherals, nova::hw::Link &link) {
    nova::BehaviourEngine engine;
    nova::Outbox<200> outbox;
    nova::ConnectionPolicy policy;

    uint32_t next_heartbeat_ms = now_ms();
    uint32_t retry_at_ms = 0;
    bool link_open = false;

    while (true) {
        const uint32_t tick = now_ms();

        // -- Reconnect ------------------------------------------------------
        if (!link.connected()) {
            if (link_open) {
                link_open = false;
                const nova::Decision decision =
                    policy.on_failure(link.last_failure(), random_unit());
                if (decision.disposition == nova::Disposition::Reprovision) {
                    // The credential is not going to start working. Erasing
                    // it and restarting returns the device to provisioning,
                    // where its owner can see a claim code instead of a robot
                    // that does nothing and explains nothing.
                    ESP_LOGE(kTag, "credential rejected repeatedly, returning to provisioning");
                    nova::hw::Store::forget();
                    esp_restart();
                }
                retry_at_ms = tick + decision.delay_ms;
                ESP_LOGW(kTag, "reconnecting in %u ms", decision.delay_ms);
            } else if (static_cast<int32_t>(tick - retry_at_ms) >= 0) {
                if (link.open() == ESP_OK) {
                    link_open = true;
                }
            }
        } else {
            if (!link_open) {
                link_open = true;
                policy.on_connected();
            }
            // A socket that is open and silent is the failure that does not
            // announce itself; TCP keeps accepting writes into nothing.
            if (nova::ConnectionPolicy::is_stale(link.silent_for(tick))) {
                ESP_LOGW(kTag, "link silent, tearing it down");
                link.close();
            }
        }

        // -- Inbound --------------------------------------------------------
        nova::Inbound *frame = nullptr;
        while (xQueueReceive(g_inbound, &frame, 0) == pdTRUE) {
            // Owned from here; freed on every path out of this block.
            const std::unique_ptr<nova::Inbound> owned(frame);

            if (!owned->valid()) {
                // Rejected frames are reported back rather than dropped, so a
                // protocol bug on either side is visible from the other end.
                ESP_LOGW(kTag, "rejected inbound frame: %s", owned->rejection.c_str());
                link.send(nova::encode_result(
                    nova::CommandResult{owned->id, false, owned->rejection}, frame_id()));
                continue;
            }
            engine.apply(nova::intent_for(*owned), tick);
            if (owned->kind == nova::InboundKind::HeadMove) {
                // The pose still goes through the engine's next update: a
                // command is a request, and a device being picked up should
                // not obey one.
                link.send(nova::encode_result(nova::CommandResult{owned->id, true, std::nullopt},
                                              frame_id()));
            }
        }

        // -- Observe and act ------------------------------------------------
        nova::Observation observation;
        observation.now_ms = tick;
        observation.distance_cm = peripherals.rangefinder.read();
        observation.backend_connected = link.connected();

        const nova::Action action = engine.update(observation);

        if (action.move_head) {
            peripherals.head.move_to(action.pose, tick);
        }
        peripherals.head.tick(tick);

        if (action.redraw_face) {
            // Face rendering is the remaining piece of this phase. Until the
            // panel driver exists the state change is logged rather than
            // silently dropped, so a bring-up session can see the engine
            // working without a screen attached.
            ESP_LOGI(kTag, "face -> %s", state_name(action.state));
        }

        // -- Telemetry ------------------------------------------------------
        if (static_cast<int32_t>(tick - next_heartbeat_ms) >= 0) {
            next_heartbeat_ms = tick + kHeartbeatIntervalMs;

            nova::Heartbeat heartbeat;
            heartbeat.uptime_seconds = uptime_seconds();
            heartbeat.state = state_name(action.state);
            heartbeat.firmware_version = CONFIG_NOVA_FIRMWARE_VERSION;
            if (nova::hw::Wifi::connected()) {
                // Left absent rather than sent as 0 when disassociated. Zero
                // is a valid dBm reading and would show on the dashboard as a
                // device with a perfect signal at the moment it lost the
                // network.
                heartbeat.wifi_rssi = nova::hw::Wifi::rssi();
            }
            link.send(nova::encode_heartbeat(heartbeat, frame_id()));

            nova::TelemetryEvent event;
            event.event_type = "device.state";
            event.recorded_at = device_timestamp();
            event.state = heartbeat.state;
            event.wifi_rssi = heartbeat.wifi_rssi;
            event.uptime_seconds = heartbeat.uptime_seconds;
            if (observation.distance_cm.has_value()) {
                event.distance_cm = observation.distance_cm;
            }
            outbox.push(event);
        }

        // Flush whatever the outbox holds. Peek then release, so a send that
        // fails leaves the batch queued rather than losing it.
        //
        // Only what the frame actually carried is released. The server caps a
        // batch at 100 events *and* at 16 KiB, and which binds depends on how
        // full each event is: sparse ones all fit, but the device.state
        // events queued below carry six fields, and a hundred of those
        // overshoot. Releasing the whole peek would discard the remainder.
        if (link.connected() && !outbox.empty()) {
            const auto batch = outbox.peek();
            const nova::BatchFrame frame = nova::encode_batch(batch, frame_id());

            if (frame.empty()) {
                // One event too large to send on its own. Dropped, loudly: a
                // device that keeps retrying it never sends anything again,
                // which costs all telemetry rather than one event.
                ESP_LOGE(kTag, "dropping an event that cannot fit in a frame");
                outbox.release(1);
            } else if (link.send(frame.json) == ESP_OK) {
                outbox.release(frame.included);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(kTickMs));
    }
}

}  // namespace

extern "C" void app_main() {
    ESP_ERROR_CHECK(nova::hw::Store::init());

    Peripherals peripherals;
    start_peripherals(peripherals);

    g_inbound = xQueueCreate(kInboundQueueDepth, sizeof(nova::Inbound *));
    if (g_inbound == nullptr) {
        ESP_LOGE(kTag, "cannot allocate the inbound queue");
        return;
    }

    const nova::hw::WifiCredentials credentials = nova::hw::Store::wifi();
    const std::string token = nova::hw::Store::token();

    if (!credentials.complete() || token.empty()) {
        // Provisioning is a Phase 2 flow that needs the display to show a
        // claim code, which does not exist yet. Saying so is better than
        // pretending: a device that boots into a state it cannot leave should
        // at least name the state.
        ESP_LOGE(kTag, "not provisioned: no %s stored",
                 credentials.complete() ? "device token" : "wifi credentials");
        return;
    }

    ESP_ERROR_CHECK(nova::hw::Wifi::init());
    if (nova::hw::Wifi::connect(credentials) != ESP_OK) {
        ESP_LOGE(kTag, "wifi unavailable at boot; running offline");
    }

    nova::hw::Link link;
    if (link.begin(kBackendUrl, token, on_frame) != ESP_OK) {
        ESP_LOGE(kTag, "cannot build the backend link");
        return;
    }

    run(peripherals, link);
}
