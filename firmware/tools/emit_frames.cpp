// Print every kind of outbound frame the firmware can produce, as JSON.
//
// Not a test on its own. It is the device half of a cross-language contract
// check: `scripts/check_protocol_contract.py` runs this and validates each
// line against the Pydantic schema in
// `services/api/src/nova/schemas/protocol.py`.
//
// Two implementations of one wire format in two languages will drift. This is
// what notices.

#include <cstdio>
#include <string>
#include <vector>

#include "nova/protocol.hpp"

using namespace nova;

namespace {

void emit(const char *name, const std::string &json) {
    // One frame per line, name first: the checker reads it as a stream so a
    // failure names the frame that broke rather than "output invalid".
    std::printf("%s\t%s\n", name, json.c_str());
}

TelemetryEvent presence() {
    TelemetryEvent event;
    event.event_type = "person_detected";
    event.recorded_at = "2026-09-07T08:15:00Z";
    event.distance_cm = 62;
    event.state = "curious";
    return event;
}

}  // namespace

int main() {
    const FrameId id = "00000001-0000-4000-8000-000000000000";

    Heartbeat minimal;
    minimal.uptime_seconds = 42;
    minimal.state = "idle";
    emit("heartbeat_minimal", encode_heartbeat(minimal, id));

    Heartbeat full;
    full.uptime_seconds = 86400;
    full.state = "listening";
    full.battery_percent = 73;
    full.temperature_c = 24.5;
    full.wifi_rssi = -58;
    full.firmware_version = "0.1.0";
    emit("heartbeat_full", encode_heartbeat(full, id));

    // Every state the behaviour engine can report, so a name the server's
    // column cannot hold is caught here rather than at insert time.
    const State states[] = {State::Idle,     State::Listening, State::Thinking,
                            State::Speaking, State::Curious,   State::Happy,
                            State::Confused, State::Alert,     State::Sleeping,
                            State::Offline};
    for (const State state : states) {
        Heartbeat beat;
        beat.uptime_seconds = 1;
        beat.state = state_name(state);
        emit((std::string("heartbeat_state_") + state_name(state)).c_str(),
             encode_heartbeat(beat, id));
    }

    TelemetryEvent bare;
    bare.event_type = "heartbeat";
    bare.recorded_at = "2026-09-07T08:15:00Z";
    emit("event_minimal", encode_event(bare, id));

    emit("event_full", encode_event(presence(), id));

    TelemetryEvent pose;
    pose.event_type = "head_moved";
    pose.recorded_at = "2026-09-07T08:15:01Z";
    pose.head_yaw = -90;
    pose.head_pitch = 45;
    pose.battery_percent = 0;
    pose.temperature_c = -40.0;
    pose.wifi_rssi = -120;
    pose.uptime_seconds = 0;
    emit("event_extremes", encode_event(pose, id));

    emit("batch_one", encode_batch({presence()}, id));
    emit("batch_many", encode_batch({presence(), bare, pose}, id));

    CommandResult ok;
    ok.command_id = "00000002-0000-4000-8000-000000000000";
    ok.ok = true;
    emit("result_ok", encode_result(ok, id));

    CommandResult failed;
    failed.command_id = "00000002-0000-4000-8000-000000000000";
    failed.ok = false;
    failed.detail = "servo stalled";
    emit("result_failed", encode_result(failed, id));

    return 0;
}
