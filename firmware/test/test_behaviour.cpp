// The behaviour engine.
//
// Every case here is about a way a desk companion can be annoying: greeting
// you twice, forgetting you while you sit still, talking while being carried,
// twitching between states because a sensor reading wobbled by a centimetre.
//
// The engine takes a clock as input rather than reading one, so these run in
// microseconds instead of minutes.

#include "check.hpp"
#include "nova/behaviour.hpp"

using namespace nova;

namespace {

/// An observation with sensible defaults, so each test states only what it
/// is actually about.
Observation at(uint32_t now_ms, std::optional<int> distance = std::nullopt) {
    Observation observation;
    observation.now_ms = now_ms;
    observation.distance_cm = distance;
    return observation;
}

/// Run the engine over a span of time with a fixed observation, the way the
/// real loop would. Returns the last action.
Action hold(BehaviourEngine &engine, uint32_t from_ms, uint32_t to_ms,
            std::optional<int> distance, uint32_t step_ms = 100) {
    Action action{};
    for (uint32_t t = from_ms; t <= to_ms; t += step_ms) {
        action = engine.update(at(t, distance));
    }
    return action;
}

}  // namespace

int main() {
    std::printf("behaviour\n");

    CASE("starts idle and level") {
        BehaviourEngine engine;
        const auto action = engine.update(at(0));
        CHECK(action.state == State::Idle);
        CHECK(!engine.present());
    }

    CASE("a brief reading is not an arrival") {
        // Somebody walking past, or a hand crossing the sensor. Reacting to
        // this makes the device look twitchy.
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs - 200, 50);
        CHECK(!engine.present());
        CHECK(engine.state() == State::Idle);
    }

    CASE("a sustained reading is an arrival") {
        BehaviourEngine engine;
        const auto action = hold(engine, 0, kPresenceDebounceMs + 300, 50);
        CHECK(engine.present());
        CHECK(action.state == State::Curious);
    }

    CASE("arriving is noticed once, not every tick") {
        // The face is redrawn on change. Without that, an AMOLED redraws
        // continuously while somebody sits at their desk, which is the
        // largest power draw on the board.
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs + 300, 50);

        int redraws = 0;
        for (uint32_t t = kPresenceDebounceMs + 400; t < kPresenceDebounceMs + 3000; t += 100) {
            if (engine.update(at(t, 50)).redraw_face) {
                ++redraws;
            }
        }
        CHECK_INT(redraws, 0);
    }

    CASE("presence has hysteresis at the boundary") {
        // Somebody sitting at almost exactly the threshold. Without a gap
        // between the near and far limits, a centimetre of sensor noise
        // makes NOVA greet and forget them repeatedly.
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs + 300, kPresenceNearCm - 5);
        CHECK(engine.present());

        // Drifting just past the near threshold must not clear presence.
        hold(engine, 2000, 2000 + kPresenceDebounceMs + 300, kPresenceNearCm + 10);
        CHECK(engine.present());

        // Genuinely leaving does.
        hold(engine, 6000, 6000 + kPresenceDebounceMs + 300, kPresenceFarCm + 20);
        CHECK(!engine.present());
    }

    CASE("the far threshold is beyond the near one") {
        // The property the hysteresis depends on.
        CHECK(kPresenceFarCm > kPresenceNearCm);
    }

    CASE("no reading at all means nobody is there") {
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs + 300, 50);
        CHECK(engine.present());

        // The ToF returns nothing when the nearest surface is out of range.
        hold(engine, 3000, 3000 + kPresenceDebounceMs + 300, std::nullopt);
        CHECK(!engine.present());
    }

    CASE("being picked up beats everything else") {
        // A device talking calmly while held upside down is the most
        // obviously-broken thing it could do.
        BehaviourEngine engine;
        engine.apply(Intent::Speak, 0);
        CHECK(engine.state() == State::Speaking);

        Observation observation = at(100, 30);
        observation.picked_up = true;
        observation.touched = true;
        CHECK(engine.update(observation).state == State::Alert);
    }

    CASE("intents are declined while the device is held") {
        BehaviourEngine engine;
        Observation observation = at(0);
        observation.picked_up = true;
        engine.update(observation);
        CHECK(engine.state() == State::Alert);

        // The backend does not know the device is in someone's hand.
        engine.apply(Intent::Speak, 100);
        CHECK(engine.state() == State::Alert);

        // Rest is the one intent that gets through, so there is a way out.
        engine.apply(Intent::Rest, 200);
        CHECK(engine.state() == State::Idle);
    }

    CASE("losing the backend is a state, not a failure") {
        BehaviourEngine engine;
        Observation observation = at(0, 40);
        observation.backend_connected = false;
        CHECK(engine.update(observation).state == State::Offline);
    }

    CASE("offline still reacts to the room") {
        // The premise of the whole device: it keeps behaving when the
        // network does not.
        BehaviourEngine engine;
        Observation observation = at(0, 40);
        observation.backend_connected = false;

        for (uint32_t t = 0; t <= kPresenceDebounceMs + 300; t += 100) {
            observation.now_ms = t;
            engine.update(observation);
        }
        CHECK(engine.present());
        // Attentive, not resting: it has noticed somebody.
        CHECK_INT(engine.pose().pitch, -12);
    }

    CASE("a reply is not interrupted by presence flickering") {
        // Cutting NOVA off mid-sentence because the sensor wobbled would be
        // worse than any pose being briefly wrong.
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs + 300, 50);
        engine.apply(Intent::Speak, 3000);

        hold(engine, 3100, 3100 + kPresenceDebounceMs + 300, kPresenceFarCm + 50);
        CHECK(engine.state() == State::Speaking);
    }

    CASE("attention lapses back to idle") {
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs + 300, 50);
        CHECK(engine.state() == State::Curious);

        const auto action = hold(engine, 2000, 2000 + kAttentionSpanMs + 500, 50);
        CHECK(action.state == State::Idle);
    }

    CASE("it sleeps after long enough with nothing happening") {
        BehaviourEngine engine;
        const auto action = hold(engine, 0, kSleepAfterMs + 2000, std::nullopt, 1000);
        CHECK(action.state == State::Sleeping);
    }

    CASE("an interaction postpones sleep") {
        BehaviourEngine engine;
        hold(engine, 0, kSleepAfterMs - 5000, std::nullopt, 1000);
        CHECK(engine.state() != State::Sleeping);

        Observation touch = at(kSleepAfterMs - 4000);
        touch.touched = true;
        engine.update(touch);

        // Well past the original deadline, but the clock restarted.
        const auto action = hold(engine, kSleepAfterMs - 3000, kSleepAfterMs + 3000,
                                 std::nullopt, 1000);
        CHECK(action.state != State::Sleeping);
    }

    CASE("head movement is reported only when the pose changes") {
        // Re-sending an identical position to a servo makes it hum.
        BehaviourEngine engine;
        hold(engine, 0, kPresenceDebounceMs + 300, 50);

        int moves = 0;
        for (uint32_t t = kPresenceDebounceMs + 400; t < kPresenceDebounceMs + 3000; t += 100) {
            if (engine.update(at(t, 50)).move_head) {
                ++moves;
            }
        }
        CHECK_INT(moves, 0);
    }

    CASE("every state has a wire name and a face") {
        // The wire names are part of the protocol: the backend stores them
        // in device_telemetry.state and analytics groups by them.
        const State all[] = {State::Idle,    State::Listening, State::Thinking,
                             State::Speaking, State::Curious,  State::Happy,
                             State::Confused, State::Alert,    State::Sleeping,
                             State::Offline};
        for (const State state : all) {
            CHECK(state_name(state) != nullptr);
            CHECK(face_name(state) != nullptr);
            CHECK(state_name(state)[0] != '\0');
            CHECK(face_name(state)[0] != '\0');
        }
    }

    CASE("wire names match what the backend expects") {
        CHECK_STR(state_name(State::Idle), "idle");
        CHECK_STR(state_name(State::Speaking), "speaking");
        CHECK_STR(state_name(State::Offline), "offline");
    }

    CASE("poses handed to the driver are always within travel") {
        // The engine is the last thing between an intent and a servo. No
        // path through it may produce an angle the mechanism cannot reach.
        BehaviourEngine engine;
        const Intent intents[] = {Intent::None,   Intent::Acknowledge, Intent::Attend,
                                  Intent::Express, Intent::Speak,      Intent::Rest};
        uint32_t clock = 0;

        for (const Intent intent : intents) {
            for (const std::optional<int> distance :
                 {std::optional<int>{}, std::optional<int>{10}, std::optional<int>{300}}) {
                engine.apply(intent, clock += 100);
                const auto action = engine.update(at(clock, distance));
                CHECK(action.pose.yaw >= kMinYaw && action.pose.yaw <= kMaxYaw);
                CHECK(action.pose.pitch >= kMinPitch && action.pose.pitch <= kMaxPitch);
            }
        }
    }

    return check::report("behaviour");
}
