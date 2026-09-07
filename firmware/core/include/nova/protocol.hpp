// The device protocol, device side.
//
// This mirrors `services/api/src/nova/schemas/protocol.py`. The two are
// written in different languages against the same wire format, and nothing
// but tests keeps them honest -- so the tests here run against frames
// captured from the Python schema rather than from this file's own idea of
// what a frame looks like.
//
// Two rules carried over from the server:
//
//   * **Unknown frames are rejected, not guessed at.** A command that is
//     almost valid moves real servos.
//   * **Every frame carries a version.** The firmware ships inside a physical
//     object that may not be reflashed for months, so the backend will
//     eventually be talking to an older protocol than it prefers.
//
// Pure C++ apart from cJSON, which ESP-IDF already bundles -- so this
// compiles and is tested on a host with no toolchain.

#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "nova/behaviour.hpp"
#include "nova/geometry.hpp"

namespace nova {

inline constexpr int kProtocolVersion = 1;

// A device sends small JSON. The server rejects anything larger than 16 KiB
// before parsing it, and the device should not be generating it either.
inline constexpr size_t kMaxFrameBytes = 16384;

/// A UUID, as text. Not parsed into bits: the device only ever echoes these
/// back, and 16 bytes of struct plus a formatter buys nothing.
using FrameId = std::string;

// ---------------------------------------------------------------------------
// Outbound: device -> server
// ---------------------------------------------------------------------------

/// Periodic liveness and vitals.
struct Heartbeat {
    uint32_t uptime_seconds = 0;
    std::string state = "idle";
    std::optional<int> battery_percent;
    std::optional<double> temperature_c;
    std::optional<int> wifi_rssi;
    std::optional<std::string> firmware_version;
};

/// One structured observation.
///
/// ``recorded_at`` is the device's own clock, in RFC 3339. It is distinct
/// from arrival time so that a batch queued during an outage and flushed on
/// reconnect is not mistaken for a burst of live activity -- which is the
/// difference between "you were at your desk all evening" and "the wifi came
/// back at 21:04".
struct TelemetryEvent {
    std::string event_type;
    std::string recorded_at;

    std::optional<int> battery_percent;
    std::optional<double> temperature_c;
    std::optional<int> distance_cm;
    std::optional<int> head_yaw;
    std::optional<int> head_pitch;
    std::optional<int> wifi_rssi;
    std::optional<uint32_t> uptime_seconds;
    std::optional<std::string> state;
};

/// The outcome of a command the server sent.
struct CommandResult {
    FrameId command_id;
    bool ok = true;
    std::optional<std::string> detail;
};

/// Encode an outbound frame. ``id`` is the frame's own identifier.
std::string encode_heartbeat(const Heartbeat &heartbeat, const FrameId &id);
std::string encode_event(const TelemetryEvent &event, const FrameId &id);
std::string encode_batch(const std::vector<TelemetryEvent> &events, const FrameId &id);
std::string encode_result(const CommandResult &result, const FrameId &id);

// ---------------------------------------------------------------------------
// Inbound: server -> device
// ---------------------------------------------------------------------------

/// What kind of frame arrived.
enum class InboundKind : uint8_t {
    // Parsing failed, or the frame was not one this firmware understands.
    // Distinct from every valid kind so a caller cannot forget to check.
    Invalid,
    HeadMove,
    Expression,
    Speak,
    Sleep,
    Restart,
    Ack,
    Error,
};

/// A decoded inbound frame.
///
/// One struct rather than a variant: the set is small, fixed by the protocol,
/// and a tagged union here would cost more in access ceremony than it saves.
/// Fields not belonging to ``kind`` are left at their defaults.
struct Inbound {
    InboundKind kind = InboundKind::Invalid;
    FrameId id;

    // Why the frame was rejected. Populated only when kind is Invalid, and
    // sent back to the server as an error frame so a firmware or backend bug
    // is visible from the other end rather than being silently dropped.
    std::string rejection;

    // head.move
    HeadPose pose{};
    int duration_ms = 500;

    // expression.set
    std::string emotion;
    double intensity = 1.0;

    // speaker.play
    std::string text;
    std::optional<std::string> speak_emotion;

    // device.sleep
    uint32_t sleep_seconds = 0;

    // ack / error
    FrameId ref;
    std::string code;
    std::string message;

    bool valid() const { return kind != InboundKind::Invalid; }
};

/// Decode one inbound frame.
///
/// Never throws and never partially applies: a frame that fails any check
/// comes back as ``Invalid`` with a reason, and the caller sends an error
/// frame rather than acting on half of it.
Inbound decode(const std::string &json);

/// The intent a command implies, for the behaviour engine.
///
/// The mapping lives here so the engine never learns the wire format, and the
/// protocol never learns what a servo is.
Intent intent_for(const Inbound &frame);

}  // namespace nova
