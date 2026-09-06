#include "nova/behaviour.hpp"

namespace nova {

namespace {

/// How far NOVA turns to face something it has noticed.
///
/// A single ToF sensor pointed forward cannot tell direction -- it measures
/// distance along one ray. So "attend" is a small, deliberately ambiguous
/// tilt upward rather than a turn towards a bearing the device does not
/// actually know. Pretending to track someone it cannot locate is the kind
/// of detail that reads as broken the third time you see it.
constexpr HeadPose kAttentivePose{0, -12};
constexpr HeadPose kRestingPose{0, 15};
constexpr HeadPose kNeutralPose{0, 0};

}  // namespace

bool BehaviourEngine::update_presence(const Observation &observation) {
    // Hysteresis, so somebody sitting near the boundary does not flicker.
    const int threshold = present_ ? kPresenceFarCm : kPresenceNearCm;
    const bool reading =
        observation.distance_cm.has_value() && *observation.distance_cm <= threshold;

    if (reading != presence_candidate_) {
        // The reading changed; start timing it.
        presence_candidate_ = reading;
        presence_since_ = observation.now_ms;
        return present_;
    }

    if (reading == present_) {
        // Already settled here.
        presence_since_.reset();
        return present_;
    }

    // A reading has to hold for the debounce window before it counts. A hand
    // passing the sensor is not somebody arriving.
    if (presence_since_ && observation.now_ms - *presence_since_ >= kPresenceDebounceMs) {
        present_ = reading;
        presence_since_.reset();
    }
    return present_;
}

void BehaviourEngine::enter(State next, uint32_t now_ms) {
    if (state_ == next) {
        return;
    }
    state_ = next;
    state_entered_ms_ = now_ms;
}

Action BehaviourEngine::update(const Observation &observation) {
    const State previous_state = state_;
    const HeadPose previous_pose = pose_;

    const bool was_present = present_;
    const bool now_present = update_presence(observation);
    const bool arrived = now_present && !was_present;

    if (arrived || observation.touched || observation.picked_up) {
        last_interaction_ms_ = observation.now_ms;
    }

    // Order matters below: earlier branches win. Being picked up beats
    // everything, because a device talking calmly while held upside down is
    // the single most obviously-broken thing it could do.
    if (observation.picked_up) {
        enter(State::Alert, observation.now_ms);
        pose_ = kNeutralPose;
    } else if (!observation.backend_connected) {
        // Offline is a state, not an error. The device keeps reacting to the
        // room; it just cannot think.
        enter(State::Offline, observation.now_ms);
        pose_ = now_present ? kAttentivePose : kNeutralPose;
    } else if (observation.touched) {
        enter(State::Happy, observation.now_ms);
        pose_ = kAttentivePose;
    } else if (arrived) {
        enter(State::Curious, observation.now_ms);
        pose_ = kAttentivePose;
    } else if (state_ == State::Speaking || state_ == State::Thinking ||
               state_ == State::Listening) {
        // Mid-exchange. Leave it alone: the backend drives these, and
        // interrupting a reply because presence flickered would cut NOVA off
        // mid-sentence.
        pose_ = kAttentivePose;
    } else if (observation.now_ms - last_interaction_ms_ >= kSleepAfterMs) {
        enter(State::Sleeping, observation.now_ms);
        pose_ = kRestingPose;
    } else if (observation.now_ms - state_entered_ms_ >= kAttentionSpanMs) {
        // Attention lapsed. Back to idle either way; the pose differs
        // because a device with somebody in front of it should look level,
        // not settle back as though the room were empty.
        enter(State::Idle, observation.now_ms);
        pose_ = now_present ? kNeutralPose : kRestingPose;
    }

    return Action{
        .state = state_,
        .pose = pose_,
        .redraw_face = state_ != previous_state,
        .move_head = !(pose_ == previous_pose),
    };
}

void BehaviourEngine::apply(Intent intent, uint32_t now_ms) {
    // An intent is a request, not an order. The engine knows things the
    // backend does not -- that the device is in someone's hand, for one --
    // and declining is part of its job.
    if (state_ == State::Alert && intent != Intent::Rest) {
        return;
    }

    switch (intent) {
        case Intent::None:
            break;
        case Intent::Acknowledge:
            enter(State::Happy, now_ms);
            pose_ = kAttentivePose;
            break;
        case Intent::Attend:
            enter(State::Listening, now_ms);
            pose_ = kAttentivePose;
            break;
        case Intent::Express:
            enter(State::Curious, now_ms);
            pose_ = kAttentivePose;
            break;
        case Intent::Speak:
            enter(State::Speaking, now_ms);
            pose_ = kAttentivePose;
            break;
        case Intent::Rest:
            enter(State::Idle, now_ms);
            pose_ = kRestingPose;
            break;
    }
    last_interaction_ms_ = now_ms;
}

const char *state_name(State state) {
    switch (state) {
        case State::Idle: return "idle";
        case State::Listening: return "listening";
        case State::Thinking: return "thinking";
        case State::Speaking: return "speaking";
        case State::Curious: return "curious";
        case State::Happy: return "happy";
        case State::Confused: return "confused";
        case State::Alert: return "alert";
        case State::Sleeping: return "sleeping";
        case State::Offline: return "offline";
    }
    return "idle";  // Unreachable for a valid enum; keeps the compiler quiet.
}

const char *face_name(State state) {
    // Several states share a face on purpose. "Thinking" and "confused" look
    // alike because the difference is legible from context and inventing a
    // distinct animation for every state produces a device that seems to be
    // emoting constantly.
    switch (state) {
        case State::Idle: return "idle";
        case State::Listening: return "listening";
        case State::Thinking:
        case State::Confused: return "thinking";
        case State::Speaking: return "speaking";
        case State::Curious: return "curious";
        case State::Happy: return "happy";
        case State::Alert: return "alert";
        case State::Sleeping: return "sleeping";
        case State::Offline: return "offline";
    }
    return "idle";
}

}  // namespace nova
