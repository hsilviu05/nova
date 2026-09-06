// The behaviour engine.
//
// This is the boundary the whole architecture rests on: **the language model
// never reaches GPIO.** It emits a high-level intent -- "look curious",
// "someone arrived" -- and this decides what that means for a servo, a face,
// and a speaker. Nothing upstream of here can address a pin, and nothing
// downstream of here makes a decision.
//
// Pure C++, no ESP-IDF. It takes observations and a clock, returns actions,
// and touches no hardware, which is why it can be tested exhaustively on a
// host with no device attached.

#pragma once

#include <cstdint>
#include <optional>

#include "nova/geometry.hpp"

namespace nova {

/// What NOVA is doing. Drives the face, the head, and whether it is
/// listening.
enum class State : uint8_t {
    Idle,
    Listening,
    Thinking,
    Speaking,
    Curious,
    Happy,
    Confused,
    Alert,
    Sleeping,
    // Not a mood: the backend is unreachable and NOVA is running on its own.
    Offline,
};

/// What the backend, or the world, has asked for.
///
/// Deliberately coarse. A richer vocabulary would tempt the model into
/// choreographing movement, which is exactly the coupling this prevents.
enum class Intent : uint8_t {
    None,
    Acknowledge,
    Attend,     // Turn towards, pay attention
    Express,    // Show an emotion
    Speak,
    Rest,
};

/// Everything the engine knows about the world at one instant.
struct Observation {
    // Milliseconds since boot. Monotonic; the engine never reads a wall
    // clock, so a time sync mid-run cannot make it think an hour passed.
    uint32_t now_ms = 0;
    // Nearest object in centimetres, or absent if nothing is in range.
    std::optional<int> distance_cm;
    bool touched = false;
    bool picked_up = false;   // From the IMU
    bool backend_connected = true;
};

/// One thing to do. The engine returns these; the caller performs them.
struct Action {
    State state = State::Idle;
    HeadPose pose{};
    // True when the face should be redrawn. Redrawing an AMOLED costs power
    // and the panel is the biggest draw on the board, so it is done on
    // change rather than every tick.
    bool redraw_face = false;
    bool move_head = false;
};

// -- Tunables ---------------------------------------------------------------
//
// Timings a person actually notices. Numbers chosen for how a desk object
// should behave, not for what is convenient to implement.

// Closer than this and somebody is deliberately near the device, not just
// walking past.
inline constexpr int kPresenceNearCm = 70;
// Hysteresis: presence has to fall well outside the near threshold before it
// counts as gone. Without a gap, someone sitting at exactly 70 cm makes NOVA
// flicker between greeting them and forgetting them.
inline constexpr int kPresenceFarCm = 100;
// A reading has to persist this long before it counts. A hand passing the
// sensor is not an arrival.
inline constexpr uint32_t kPresenceDebounceMs = 800;
// Quiet for this long and NOVA goes back to idle.
inline constexpr uint32_t kAttentionSpanMs = 12000;
// Nothing at all for this long and it sleeps.
inline constexpr uint32_t kSleepAfterMs = 300000;  // five minutes

/// Decides what NOVA does, from what it can observe.
///
/// A class rather than a free function because presence detection is
/// inherently stateful -- debouncing needs to remember when a reading
/// started -- and hiding that in a static would make it untestable.
class BehaviourEngine {
   public:
    /// Advance the engine and return what to do.
    Action update(const Observation &observation);

    /// Apply an intent from the backend.
    ///
    /// Intents are requests, not commands: the engine may decline. A `Speak`
    /// while the device is being picked up is dropped, because talking to
    /// someone's hand is worse than staying quiet.
    void apply(Intent intent, uint32_t now_ms);

    State state() const { return state_; }
    HeadPose pose() const { return pose_; }
    bool present() const { return present_; }

   private:
    void enter(State next, uint32_t now_ms);
    bool update_presence(const Observation &observation);

    State state_ = State::Idle;
    HeadPose pose_{};

    bool present_ = false;
    // When the current candidate presence reading began, for debouncing.
    std::optional<uint32_t> presence_since_;
    bool presence_candidate_ = false;

    uint32_t state_entered_ms_ = 0;
    uint32_t last_interaction_ms_ = 0;
};

/// The face to draw for a state. Kept separate from the engine so the
/// rendering layer depends on this and not on the state machine's internals.
const char *face_name(State state);

/// The wire name for a state, as it appears in telemetry.
///
/// Must match what the backend stores in `device_telemetry.state`, so these
/// strings are part of the protocol, not a display detail.
const char *state_name(State state);

}  // namespace nova
