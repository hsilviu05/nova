// The device protocol codec.
//
// The inbound cases run against `fixtures/inbound.json`, which is **generated
// from the Python schema**, not written by hand here. That is the whole point:
// two implementations of one wire format in two languages, and a fixture
// invented on this side would only prove the decoder agrees with the decoder.
//
// Regenerate with:
//   python scripts/emit_protocol_fixtures.py
//
// The other direction -- frames this code produces, validated by Pydantic --
// is checked by scripts/check_protocol_contract.py, which CI runs.

#include <fstream>
#include <sstream>
#include <string>

#include "cJSON.h"
#include "check.hpp"
#include "nova/protocol.hpp"

using namespace nova;

namespace {

/// Load one named frame from the generated fixture file.
std::string fixture(const char *name) {
    static std::string contents = [] {
        std::ifstream file(std::string(FIXTURE_DIR) + "/inbound.json");
        std::stringstream buffer;
        buffer << file.rdbuf();
        return buffer.str();
    }();

    cJSON *root = cJSON_Parse(contents.c_str());
    if (root == nullptr) {
        std::printf("\n    FIXTURES MISSING OR UNPARSEABLE at %s\n", FIXTURE_DIR);
        return "";
    }

    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    std::string raw = cJSON_IsString(item) ? item->valuestring : "";
    cJSON_Delete(root);
    return raw;
}

/// A minimal valid frame, for mutating in the rejection cases.
std::string frame(const std::string &body) {
    return R"({"version":1,"id":"00000001-0000-4000-8000-000000000000",)" + body + "}";
}

/// Read one field out of an encoded frame.
std::string field(const std::string &json, const char *path, const char *name) {
    cJSON *root = cJSON_Parse(json.c_str());
    const cJSON *object = path == nullptr
                              ? root
                              : cJSON_GetObjectItemCaseSensitive(root, path);
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);

    std::string out;
    if (cJSON_IsString(item)) {
        out = item->valuestring;
    } else if (cJSON_IsNumber(item)) {
        out = std::to_string(item->valuedouble);
    } else if (cJSON_IsBool(item)) {
        out = cJSON_IsTrue(item) ? "true" : "false";
    } else if (item == nullptr) {
        out = "<absent>";
    }
    cJSON_Delete(root);
    return out;
}

bool has_field(const std::string &json, const char *path, const char *name) {
    return field(json, path, name) != "<absent>";
}

}  // namespace

int main() {
    std::printf("protocol\n");

    // -- inbound, against frames the Python schema produced ----------------

    CASE("decodes a head.move from the server") {
        const Inbound in = decode(fixture("head_move"));
        CHECK(in.kind == InboundKind::HeadMove);
        CHECK_INT(in.pose.yaw, 30);
        CHECK_INT(in.pose.pitch, -15);
        CHECK_INT(in.duration_ms, 800);
        CHECK_STR(in.id.c_str(), "00000001-0000-4000-8000-000000000000");
    }

    CASE("the server sends its defaults explicitly") {
        // Pydantic serialises duration_ms even when it was defaulted, so the
        // device never has to guess what the server meant.
        const Inbound in = decode(fixture("head_move_defaults"));
        CHECK(in.kind == InboundKind::HeadMove);
        CHECK_INT(in.duration_ms, 500);
    }

    CASE("decodes an expression") {
        const Inbound in = decode(fixture("expression"));
        CHECK(in.kind == InboundKind::Expression);
        CHECK_STR(in.emotion.c_str(), "curious");
        CHECK(in.intensity > 0.59 && in.intensity < 0.61);
    }

    CASE("decodes an expression at full intensity") {
        const Inbound in = decode(fixture("expression_defaults"));
        CHECK(in.kind == InboundKind::Expression);
        CHECK(in.intensity > 0.99);
    }

    CASE("decodes speech with an emotion") {
        const Inbound in = decode(fixture("speak"));
        CHECK(in.kind == InboundKind::Speak);
        CHECK_STR(in.text.c_str(), "Hello there.");
        CHECK(in.speak_emotion.has_value());
        CHECK_STR(in.speak_emotion->c_str(), "happy");
    }

    CASE("a null emotion is absent, not the string \"null\"") {
        // Pydantic emits `"emotion": null` rather than omitting the key. A
        // decoder treating null as a string would set the face to "null".
        const Inbound in = decode(fixture("speak_no_emotion"));
        CHECK(in.kind == InboundKind::Speak);
        CHECK(!in.speak_emotion.has_value());
    }

    CASE("decodes sleep and restart") {
        const Inbound sleep = decode(fixture("sleep"));
        CHECK(sleep.kind == InboundKind::Sleep);
        CHECK_INT(static_cast<long long>(sleep.sleep_seconds), 300);

        const Inbound restart = decode(fixture("restart"));
        CHECK(restart.kind == InboundKind::Restart);
    }

    CASE("decodes ack and error") {
        const Inbound ack = decode(fixture("ack"));
        CHECK(ack.kind == InboundKind::Ack);
        CHECK_STR(ack.ref.c_str(), "00000001-0000-4000-8000-000000000000");

        const Inbound error = decode(fixture("error"));
        CHECK(error.kind == InboundKind::Error);
        CHECK_STR(error.code.c_str(), "bad_frame");
        CHECK_STR(error.message.c_str(), "nope");
    }

    // -- rejection ---------------------------------------------------------

    CASE("rejects a frame that is not JSON") {
        CHECK(!decode("").valid());
        CHECK(!decode("{").valid());
        CHECK(!decode("not json at all").valid());
        CHECK(!decode("[1,2,3]").valid());
        CHECK(!decode("null").valid());
    }

    CASE("rejects a frame with no version") {
        CHECK(!decode(R"({"id":"x","type":"ack","ref":"y"})").valid());
    }

    CASE("rejects a protocol version this firmware does not speak") {
        // The version field exists because the device may go months without
        // a reflash. Guessing at a newer frame is how a servo moves wrongly.
        const std::string future =
            R"({"version":2,"id":"x","type":"robot.command","command":"head.move",)"
            R"("payload":{"yaw":0,"pitch":0}})";
        const Inbound in = decode(future);
        CHECK(!in.valid());
        CHECK(in.rejection.find("version") != std::string::npos);
    }

    CASE("rejects an unknown frame type") {
        CHECK(!decode(frame(R"("type":"robot.selfdestruct")")).valid());
    }

    CASE("rejects an unknown command") {
        // Newer backend, older device. Survivable precisely because it is
        // rejected rather than approximated.
        const Inbound in = decode(
            frame(R"("type":"robot.command","command":"head.spin","payload":{})"));
        CHECK(!in.valid());
        CHECK(in.rejection.find("head.spin") != std::string::npos);
    }

    CASE("a rejected frame still reports the id it was rejecting") {
        // So the error frame sent back names the command that failed.
        const Inbound in = decode(
            frame(R"("type":"robot.command","command":"head.spin","payload":{})"));
        CHECK_STR(in.id.c_str(), "00000001-0000-4000-8000-000000000000");
    }

    CASE("rejects head.move outside travel limits") {
        // Rejected, not clamped. The server enforces these too, so a command
        // arriving outside them means something upstream is wrong -- and
        // quietly obeying a changed version of it hides that.
        for (const char *payload : {
                 R"({"yaw":120,"pitch":0})",
                 R"({"yaw":-120,"pitch":0})",
                 R"({"yaw":0,"pitch":60})",
                 R"({"yaw":0,"pitch":-60})",
             }) {
            const std::string json = frame(
                std::string(R"("type":"robot.command","command":"head.move","payload":)") +
                payload);
            CHECK(!decode(json).valid());
        }
    }

    CASE("accepts head.move exactly at the limits") {
        for (const char *payload : {
                 R"({"yaw":90,"pitch":45})",
                 R"({"yaw":-90,"pitch":-45})",
             }) {
            const std::string json = frame(
                std::string(R"("type":"robot.command","command":"head.move","payload":)") +
                payload);
            CHECK(decode(json).valid());
        }
    }

    CASE("rejects a fractional angle rather than truncating it") {
        // cJSON stores every number as a double. Truncating 45.7 to 45 would
        // be a malformed command being obeyed.
        const std::string json = frame(
            R"("type":"robot.command","command":"head.move","payload":{"yaw":45.7,"pitch":0})");
        CHECK(!decode(json).valid());
    }

    CASE("rejects a non-numeric angle") {
        const std::string json = frame(
            R"("type":"robot.command","command":"head.move","payload":{"yaw":"30","pitch":0})");
        CHECK(!decode(json).valid());
    }

    CASE("rejects a missing payload") {
        CHECK(!decode(frame(R"("type":"robot.command","command":"head.move")")).valid());
        CHECK(!decode(
                   frame(R"("type":"robot.command","command":"head.move","payload":5)"))
                   .valid());
    }

    CASE("rejects out-of-range intensity and duration") {
        CHECK(!decode(frame(
                   R"("type":"robot.command","command":"expression.set",)"
                   R"("payload":{"emotion":"happy","intensity":5})"))
                   .valid());
        CHECK(!decode(frame(
                   R"("type":"robot.command","command":"head.move",)"
                   R"("payload":{"yaw":0,"pitch":0,"duration_ms":999999})"))
                   .valid());
        CHECK(!decode(frame(
                   R"("type":"robot.command","command":"device.sleep",)"
                   R"("payload":{"duration_seconds":0})"))
                   .valid());
    }

    CASE("rejects empty speech") {
        CHECK(!decode(frame(
                   R"("type":"robot.command","command":"speaker.play","payload":{"text":""})"))
                   .valid());
    }

    CASE("rejects a frame larger than the server would accept") {
        // Refused before parsing. A device with a few hundred kilobytes of
        // heap must not attempt an arbitrarily large parse.
        std::string huge = frame(
            R"("type":"robot.command","command":"speaker.play","payload":{"text":")" +
            std::string(kMaxFrameBytes + 100, 'a') + R"("})");
        const Inbound in = decode(huge);
        CHECK(!in.valid());
        CHECK(in.rejection.find("too large") != std::string::npos);
    }

    // -- outbound ----------------------------------------------------------

    CASE("a heartbeat carries the version and type") {
        Heartbeat beat;
        beat.uptime_seconds = 3600;
        beat.state = "idle";
        beat.battery_percent = 82;

        const std::string json = encode_heartbeat(beat, "abc");
        CHECK_STR(field(json, nullptr, "type").c_str(), "telemetry.heartbeat");
        CHECK_STR(field(json, nullptr, "id").c_str(), "abc");
        CHECK_STR(field(json, "payload", "state").c_str(), "idle");
        CHECK(field(json, nullptr, "version").rfind("1", 0) == 0);
    }

    CASE("absent readings are omitted, not sent as null") {
        // The server's schemas use extra="forbid" with default-None, so an
        // omitted key is the correct way to say "no reading" -- and it keeps
        // frames small on a link a battery is paying for.
        Heartbeat beat;
        beat.uptime_seconds = 10;
        beat.state = "idle";

        const std::string json = encode_heartbeat(beat, "abc");
        CHECK(!has_field(json, "payload", "battery_percent"));
        CHECK(!has_field(json, "payload", "temperature_c"));
        CHECK(has_field(json, "payload", "uptime_seconds"));
    }

    CASE("an event carries its own recorded_at") {
        // The device's clock, not arrival time: a batch flushed after an
        // outage must not look like a burst of live activity.
        TelemetryEvent event;
        event.event_type = "person_detected";
        event.recorded_at = "2026-09-07T08:15:00Z";
        event.distance_cm = 62;

        const std::string json = encode_event(event, "e1");
        CHECK_STR(field(json, nullptr, "type").c_str(), "telemetry.event");
        CHECK_STR(field(json, "payload", "recorded_at").c_str(), "2026-09-07T08:15:00Z");
        CHECK_STR(field(json, "payload", "event_type").c_str(), "person_detected");
    }

    CASE("a batch is an array of events") {
        TelemetryEvent event;
        event.event_type = "person_detected";
        event.recorded_at = "2026-09-07T08:15:00Z";

        const std::string json = encode_batch({event, event, event}, "b1");
        cJSON *root = cJSON_Parse(json.c_str());
        const cJSON *payload = cJSON_GetObjectItemCaseSensitive(root, "payload");
        CHECK(cJSON_IsArray(payload));
        CHECK_INT(cJSON_GetArraySize(payload), 3);
        cJSON_Delete(root);
    }

    CASE("a command result reports success as a boolean") {
        CommandResult result;
        result.command_id = "cmd-1";
        result.ok = false;
        result.detail = "servo stalled";

        const std::string json = encode_result(result, "r1");
        CHECK_STR(field(json, nullptr, "type").c_str(), "robot.result");
        CHECK_STR(field(json, "payload", "ok").c_str(), "false");
        CHECK_STR(field(json, "payload", "detail").c_str(), "servo stalled");
    }

    CASE("every encoded frame round-trips through the decoder's checks") {
        // Not a decode -- these are outbound types the device never receives.
        // What is asserted is that they are well-formed JSON objects with the
        // three fields every frame carries.
        Heartbeat beat;
        beat.state = "idle";
        TelemetryEvent event;
        event.event_type = "x";
        event.recorded_at = "2026-09-07T08:15:00Z";
        CommandResult result;
        result.command_id = "c";

        for (const std::string &json : {
                 encode_heartbeat(beat, "1"),
                 encode_event(event, "2"),
                 encode_batch({event}, "3"),
                 encode_result(result, "4"),
             }) {
            cJSON *root = cJSON_Parse(json.c_str());
            CHECK(root != nullptr && cJSON_IsObject(root));
            cJSON_Delete(root);
            CHECK(has_field(json, nullptr, "version"));
            CHECK(has_field(json, nullptr, "id"));
            CHECK(has_field(json, nullptr, "type"));
        }
    }

    // -- the boundary ------------------------------------------------------

    CASE("commands become intents, not servo angles") {
        // The protocol never learns what a servo is; the engine never learns
        // the wire format.
        CHECK(intent_for(decode(fixture("speak"))) == Intent::Speak);
        CHECK(intent_for(decode(fixture("expression"))) == Intent::Express);
        CHECK(intent_for(decode(fixture("sleep"))) == Intent::Rest);
        CHECK(intent_for(decode(fixture("ack"))) == Intent::None);
        CHECK(intent_for(decode("garbage")) == Intent::None);
    }

    return check::report("protocol");
}
