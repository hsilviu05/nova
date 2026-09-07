#include "nova/protocol.hpp"

#include <cmath>
#include <memory>

#include "cJSON.h"

namespace nova {

namespace {

/// cJSON returns owning pointers. This makes every early return safe, which
/// matters because `decode` has a dozen of them.
using Json = std::unique_ptr<cJSON, decltype(&cJSON_Delete)>;

Json own(cJSON *raw) { return Json{raw, cJSON_Delete}; }

// -- reading ---------------------------------------------------------------

const cJSON *member(const cJSON *object, const char *name) {
    return cJSON_GetObjectItemCaseSensitive(object, name);
}

bool is_string(const cJSON *item) { return cJSON_IsString(item) && item->valuestring != nullptr; }

std::string text(const cJSON *item) { return is_string(item) ? item->valuestring : ""; }

/// Read an integer, refusing anything that is not one.
///
/// cJSON stores every number as a double, so a fractional value would
/// otherwise be silently truncated -- and a yaw of 45.7 becoming 45 is a
/// malformed command being obeyed rather than rejected.
bool read_int(const cJSON *object, const char *name, int &out) {
    const cJSON *item = member(object, name);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    double value = item->valuedouble;
    if (std::isnan(value) || std::isinf(value) || value != std::floor(value)) {
        return false;
    }
    if (value < INT32_MIN || value > INT32_MAX) {
        return false;
    }
    out = static_cast<int>(value);
    return true;
}

bool read_double(const cJSON *object, const char *name, double &out) {
    const cJSON *item = member(object, name);
    if (!cJSON_IsNumber(item) || std::isnan(item->valuedouble) ||
        std::isinf(item->valuedouble)) {
        return false;
    }
    out = item->valuedouble;
    return true;
}

/// Reject a frame, with the reason travelling back to the server.
Inbound reject(const std::string &why, const FrameId &id = {}) {
    Inbound frame;
    frame.kind = InboundKind::Invalid;
    frame.rejection = why;
    frame.id = id;
    return frame;
}

// -- writing ---------------------------------------------------------------

void add_optional(cJSON *object, const char *name, const std::optional<int> &value) {
    // Absent rather than null. The server's schemas use `extra="forbid"` and
    // default-None, so an omitted field is the correct way to say "no
    // reading", and it keeps frames small on a battery-powered link.
    if (value) {
        cJSON_AddNumberToObject(object, name, *value);
    }
}

void add_optional(cJSON *object, const char *name, const std::optional<uint32_t> &value) {
    if (value) {
        cJSON_AddNumberToObject(object, name, static_cast<double>(*value));
    }
}

void add_optional(cJSON *object, const char *name, const std::optional<double> &value) {
    if (value) {
        cJSON_AddNumberToObject(object, name, *value);
    }
}

void add_optional(cJSON *object, const char *name, const std::optional<std::string> &value) {
    if (value) {
        cJSON_AddStringToObject(object, name, value->c_str());
    }
}

/// The fields every outbound frame carries.
cJSON *new_frame(const char *type, const FrameId &id) {
    cJSON *frame = cJSON_CreateObject();
    cJSON_AddNumberToObject(frame, "version", kProtocolVersion);
    cJSON_AddStringToObject(frame, "id", id.c_str());
    cJSON_AddStringToObject(frame, "type", type);
    return frame;
}

std::string render(cJSON *frame) {
    // Unformatted: no whitespace on a link a battery is paying for.
    char *rendered = cJSON_PrintUnformatted(frame);
    std::string out = rendered ? rendered : "";
    cJSON_free(rendered);
    return out;
}

cJSON *event_payload(const TelemetryEvent &event) {
    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "event_type", event.event_type.c_str());
    cJSON_AddStringToObject(payload, "recorded_at", event.recorded_at.c_str());

    add_optional(payload, "battery_percent", event.battery_percent);
    add_optional(payload, "temperature_c", event.temperature_c);
    add_optional(payload, "distance_cm", event.distance_cm);
    add_optional(payload, "head_yaw", event.head_yaw);
    add_optional(payload, "head_pitch", event.head_pitch);
    add_optional(payload, "wifi_rssi", event.wifi_rssi);
    add_optional(payload, "uptime_seconds", event.uptime_seconds);
    add_optional(payload, "state", event.state);
    return payload;
}

// -- inbound commands ------------------------------------------------------

Inbound decode_head_move(const cJSON *payload, const FrameId &id) {
    int yaw = 0;
    int pitch = 0;
    if (!read_int(payload, "yaw", yaw) || !read_int(payload, "pitch", pitch)) {
        return reject("head.move needs integer yaw and pitch", id);
    }

    // Out of range is a rejection, not a clamp. The server already enforces
    // these limits, so a command arriving outside them means something is
    // wrong upstream -- and silently obeying a changed version of what was
    // asked hides the bug that produced it.
    if (yaw < kMinYaw || yaw > kMaxYaw || pitch < kMinPitch || pitch > kMaxPitch) {
        return reject("head.move outside travel limits", id);
    }

    int duration = 500;
    if (member(payload, "duration_ms") != nullptr &&
        !read_int(payload, "duration_ms", duration)) {
        return reject("duration_ms must be an integer", id);
    }
    if (duration < 0 || duration > 10000) {
        return reject("duration_ms outside 0-10000", id);
    }

    Inbound frame;
    frame.kind = InboundKind::HeadMove;
    frame.id = id;
    frame.pose = HeadPose::clamped(yaw, pitch);
    frame.duration_ms = duration;
    return frame;
}

Inbound decode_expression(const cJSON *payload, const FrameId &id) {
    const cJSON *emotion = member(payload, "emotion");
    if (!is_string(emotion)) {
        return reject("expression.set needs an emotion", id);
    }

    double intensity = 1.0;
    if (member(payload, "intensity") != nullptr &&
        !read_double(payload, "intensity", intensity)) {
        return reject("intensity must be a number", id);
    }
    if (intensity < 0.0 || intensity > 1.0) {
        return reject("intensity outside 0-1", id);
    }

    Inbound frame;
    frame.kind = InboundKind::Expression;
    frame.id = id;
    frame.emotion = text(emotion);
    frame.intensity = intensity;
    return frame;
}

Inbound decode_speak(const cJSON *payload, const FrameId &id) {
    const cJSON *body = member(payload, "text");
    if (!is_string(body) || text(body).empty()) {
        return reject("speaker.play needs non-empty text", id);
    }
    if (text(body).size() > 1000) {
        return reject("speaker.play text too long", id);
    }

    Inbound frame;
    frame.kind = InboundKind::Speak;
    frame.id = id;
    frame.text = text(body);

    const cJSON *emotion = member(payload, "emotion");
    if (is_string(emotion)) {
        frame.speak_emotion = text(emotion);
    }
    return frame;
}

Inbound decode_sleep(const cJSON *payload, const FrameId &id) {
    int seconds = 0;
    if (!read_int(payload, "duration_seconds", seconds)) {
        return reject("device.sleep needs duration_seconds", id);
    }
    if (seconds < 1 || seconds > 86400) {
        return reject("duration_seconds outside 1-86400", id);
    }

    Inbound frame;
    frame.kind = InboundKind::Sleep;
    frame.id = id;
    frame.sleep_seconds = static_cast<uint32_t>(seconds);
    return frame;
}

Inbound decode_command(const cJSON *root, const FrameId &id) {
    const cJSON *command = member(root, "command");
    if (!is_string(command)) {
        return reject("robot.command needs a command", id);
    }

    const cJSON *payload = member(root, "payload");
    if (!cJSON_IsObject(payload)) {
        // device.restart carries an empty object rather than nothing, so a
        // missing payload is malformed for every command.
        return reject("robot.command needs a payload object", id);
    }

    const std::string name = text(command);
    if (name == "head.move") {
        return decode_head_move(payload, id);
    }
    if (name == "expression.set") {
        return decode_expression(payload, id);
    }
    if (name == "speaker.play") {
        return decode_speak(payload, id);
    }
    if (name == "device.sleep") {
        return decode_sleep(payload, id);
    }
    if (name == "device.restart") {
        Inbound frame;
        frame.kind = InboundKind::Restart;
        frame.id = id;
        return frame;
    }
    // A command this firmware has never heard of. Newer backend, older
    // device -- exactly what the version field exists to make survivable.
    return reject("unknown command: " + name, id);
}

}  // namespace

// ---------------------------------------------------------------------------
// Outbound
// ---------------------------------------------------------------------------

std::string encode_heartbeat(const Heartbeat &heartbeat, const FrameId &id) {
    Json frame = own(new_frame("telemetry.heartbeat", id));

    cJSON *payload = cJSON_CreateObject();
    cJSON_AddNumberToObject(payload, "uptime_seconds",
                            static_cast<double>(heartbeat.uptime_seconds));
    cJSON_AddStringToObject(payload, "state", heartbeat.state.c_str());
    add_optional(payload, "battery_percent", heartbeat.battery_percent);
    add_optional(payload, "temperature_c", heartbeat.temperature_c);
    add_optional(payload, "wifi_rssi", heartbeat.wifi_rssi);
    add_optional(payload, "firmware_version", heartbeat.firmware_version);

    cJSON_AddItemToObject(frame.get(), "payload", payload);
    return render(frame.get());
}

std::string encode_event(const TelemetryEvent &event, const FrameId &id) {
    Json frame = own(new_frame("telemetry.event", id));
    cJSON_AddItemToObject(frame.get(), "payload", event_payload(event));
    return render(frame.get());
}

std::string encode_batch(const std::vector<TelemetryEvent> &events, const FrameId &id) {
    Json frame = own(new_frame("telemetry.batch", id));

    cJSON *array = cJSON_CreateArray();
    for (const TelemetryEvent &event : events) {
        cJSON_AddItemToArray(array, event_payload(event));
    }

    cJSON_AddItemToObject(frame.get(), "payload", array);
    return render(frame.get());
}

std::string encode_result(const CommandResult &result, const FrameId &id) {
    Json frame = own(new_frame("robot.result", id));

    cJSON *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "command_id", result.command_id.c_str());
    cJSON_AddBoolToObject(payload, "ok", result.ok);
    add_optional(payload, "detail", result.detail);

    cJSON_AddItemToObject(frame.get(), "payload", payload);
    return render(frame.get());
}

// ---------------------------------------------------------------------------
// Inbound
// ---------------------------------------------------------------------------

Inbound decode(const std::string &json) {
    if (json.size() > kMaxFrameBytes) {
        // Rejected before parsing, the same way the server does. A device
        // with a few hundred kilobytes of heap must not try to parse
        // something arbitrarily large.
        return reject("frame too large");
    }

    Json root = own(cJSON_ParseWithLength(json.data(), json.size()));
    if (!root || !cJSON_IsObject(root.get())) {
        return reject("not a JSON object");
    }

    int version = 0;
    if (!read_int(root.get(), "version", version)) {
        return reject("missing version");
    }
    if (version != kProtocolVersion) {
        return reject("unsupported protocol version");
    }

    const cJSON *id_item = member(root.get(), "id");
    if (!is_string(id_item)) {
        return reject("missing id");
    }
    const FrameId id = text(id_item);

    const cJSON *type = member(root.get(), "type");
    if (!is_string(type)) {
        return reject("missing type", id);
    }

    const std::string kind = text(type);
    if (kind == "robot.command") {
        return decode_command(root.get(), id);
    }
    if (kind == "ack") {
        const cJSON *ref = member(root.get(), "ref");
        if (!is_string(ref)) {
            return reject("ack needs a ref", id);
        }
        Inbound frame;
        frame.kind = InboundKind::Ack;
        frame.id = id;
        frame.ref = text(ref);
        return frame;
    }
    if (kind == "error") {
        Inbound frame;
        frame.kind = InboundKind::Error;
        frame.id = id;
        frame.code = text(member(root.get(), "code"));
        frame.message = text(member(root.get(), "message"));
        const cJSON *ref = member(root.get(), "ref");
        if (is_string(ref)) {
            frame.ref = text(ref);
        }
        return frame;
    }

    return reject("unknown frame type: " + kind, id);
}

Intent intent_for(const Inbound &frame) {
    switch (frame.kind) {
        case InboundKind::Speak:
            return Intent::Speak;
        case InboundKind::Sleep:
            return Intent::Rest;
        case InboundKind::Expression:
            // The engine decides what an emotion looks like. Passing the
            // emotion string through to a servo angle here is exactly the
            // coupling the behaviour layer exists to prevent.
            return Intent::Express;
        case InboundKind::HeadMove:
            // A direct pose is applied by the caller, not requested as an
            // intent: the server has already asked for a specific angle.
            return Intent::Attend;
        case InboundKind::Restart:
        case InboundKind::Ack:
        case InboundKind::Error:
        case InboundKind::Invalid:
            return Intent::None;
    }
    return Intent::None;
}

}  // namespace nova
