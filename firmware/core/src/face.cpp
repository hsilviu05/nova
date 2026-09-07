#include "nova/face.hpp"

namespace nova {

namespace {

constexpr int16_t kEyeCentreOffset = (kEyeWidth + kEyeGap) / 2;

// The eyes stay on the panel by construction, and it is proved here rather
// than clamped at runtime.
//
// A clamp would be an unreachable branch no test could exercise -- and worse,
// it would silently distort the face if it ever did fire, hiding the mistake
// that caused it. These assertions fail the build instead, naming the
// constant somebody widened. The panel driver can then write the geometry it
// is given without bounds-checking it.
constexpr int kWorstVerticalOffset = 16;  // Sleeping, the largest in the table.

static_assert(kPanelWidth / 2 - kEyeCentreOffset - kEyeWidth / 2 - kSaccadeRangeX >= 0,
              "the left eye can be pushed off the left edge: reduce kSaccadeRangeX, "
              "kEyeWidth or kEyeGap");
static_assert(kPanelWidth / 2 + kEyeCentreOffset + kEyeWidth / 2 + kSaccadeRangeX <= kPanelWidth,
              "the right eye can be pushed off the right edge");
static_assert(kEyeBaselineY - kEyeHeight / 2 - kSaccadeRangeY - kWorstVerticalOffset >= 0,
              "the eyes can be pushed off the top edge");
static_assert(kEyeBaselineY + kEyeHeight / 2 + kSaccadeRangeY + kWorstVerticalOffset
                  <= kPanelHeight,
              "the eyes can be pushed off the bottom edge");
static_assert(kEyeGap > 2 * kSaccadeRangeX,
              "gaze drift is larger than the gap: the eyes could meet");

uint32_t between(uint32_t low, uint32_t high, double unit) {
    const double clamped = unit < 0.0 ? 0.0 : (unit >= 1.0 ? 0.999999 : unit);
    return low + static_cast<uint32_t>(static_cast<double>(high - low) * clamped);
}

/// How far through a span, in permille, clamped at both ends.
int32_t progress(uint32_t started, uint32_t now, uint32_t duration) {
    if (duration == 0) {
        return 1000;
    }
    const uint32_t elapsed = now - started;
    if (elapsed >= duration) {
        return 1000;
    }
    return static_cast<int32_t>(static_cast<uint64_t>(elapsed) * 1000 / duration);
}

/// Height in pixels for an openness in permille.
int16_t height_for(int16_t openness) {
    const int32_t span = kEyeHeight - kEyeClosedHeight;
    const int32_t value = kEyeClosedHeight + span * clamp(openness, 0, 1000) / 1000;
    return static_cast<int16_t>(value);
}

}  // namespace

int32_t smoothstep(int32_t progress_permille) {
    const int64_t t = clamp(progress_permille, 0, 1000);
    // t^2 * (3 - 2t), scaled to permille throughout. 64-bit because the
    // intermediate reaches 10^9 and this must stay exact.
    return static_cast<int32_t>((t * t * (3000 - 2 * t)) / 1000000);
}

int16_t ease(int16_t from, int16_t to, int32_t progress_permille) {
    const int32_t eased = smoothstep(progress_permille);
    return static_cast<int16_t>(from + (to - from) * eased / 1000);
}

Expression expression_for(State state) {
    Expression e;
    switch (state) {
        case State::Idle:
            return e;

        case State::Listening:
            // Open a little wider and sit up. Attention, without alarm.
            e.openness = 1000;
            e.left_openness = 1000;
            e.vertical_offset = -8;
            return e;

        case State::Thinking:
            // Half-lidded and looking up: the posture everyone reads as
            // "working on it" rather than "not listening".
            e.openness = 620;
            e.left_openness = 620;
            e.vertical_offset = -14;
            e.radius = 30;
            return e;

        case State::Speaking:
            e.openness = 900;
            e.left_openness = 900;
            return e;

        case State::Curious:
            // One eye further open than the other. Asymmetry is the whole
            // trick -- it costs one number and reads instantly as a cocked
            // head, which two symmetrical eyes never manage.
            e.openness = 1000;
            e.left_openness = 700;
            e.vertical_offset = -6;
            return e;

        case State::Happy:
            // Squeezed shut from below and very round. This is the closest a
            // rectangle gets to the upward arc of a smiling eye, and it is
            // close enough that nobody reads it as anything else.
            e.openness = 380;
            e.left_openness = 380;
            e.vertical_offset = 10;
            e.radius = 48;
            e.colour = Rgb{120, 255, 210};
            return e;

        case State::Confused:
            e.openness = 850;
            e.left_openness = 430;
            e.vertical_offset = 4;
            e.radius = 28;
            return e;

        case State::Alert:
            // Wide and square. Roundness reads as soft, so alarm takes it
            // away.
            e.openness = 1000;
            e.left_openness = 1000;
            e.vertical_offset = -12;
            e.radius = 16;
            e.colour = Rgb{255, 255, 255};
            return e;

        case State::Sleeping:
            // Shut, and still. `animated` false is the important half: a
            // sleeping face that blinks is not asleep, and one that drifts
            // its gaze behind closed eyes is burning the panel for nothing.
            e.openness = 0;
            e.left_openness = 0;
            e.vertical_offset = 16;
            e.radius = kEyeClosedHeight / 2;
            e.colour = Rgb{0, 70, 90};
            e.animated = false;
            return e;

        case State::Offline:
            // Dimmed rather than absent. The device is still there and still
            // behaving; it just cannot reach the backend, and the face should
            // say that rather than imply a crash.
            e.openness = 760;
            e.left_openness = 760;
            e.radius = 30;
            e.colour = Rgb{110, 120, 130};
            return e;
    }
    return e;
}

Face FaceAnimator::compose(uint32_t now_ms) const {
    const int32_t t = progress(transition_started_ms_, now_ms, kExpressionMs);

    int16_t openness = ease(from_.openness, to_.openness, t);
    int16_t left_openness = ease(from_.left_openness, to_.left_openness, t);
    const int16_t offset = ease(from_.vertical_offset, to_.vertical_offset, t);
    const int16_t radius = ease(from_.radius, to_.radius, t);

    const Rgb colour{
        static_cast<uint8_t>(ease(from_.colour.r, to_.colour.r, t)),
        static_cast<uint8_t>(ease(from_.colour.g, to_.colour.g, t)),
        static_cast<uint8_t>(ease(from_.colour.b, to_.colour.b, t)),
    };

    // A blink overrides expression rather than blending with it: it is the
    // lids moving, not a change of mood, and a half-blink averaged with a
    // squint produces a shape neither one has.
    if (blinking_) {
        const int32_t bt = progress(blink_started_ms_, now_ms, kBlinkMs);
        // Down then up. Symmetrical, so the eye reaches shut exactly halfway.
        const int32_t closed = bt <= 500 ? smoothstep(bt * 2)
                                         : smoothstep((1000 - bt) * 2);
        openness = static_cast<int16_t>(openness - openness * closed / 1000);
        left_openness = static_cast<int16_t>(left_openness - left_openness * closed / 1000);
    }

    const int32_t st = progress(saccade_started_ms_, now_ms, kSaccadeMs);
    const int16_t gaze_x = ease(gaze_from_x_, gaze_to_x_, st);
    const int16_t gaze_y = ease(gaze_from_y_, gaze_to_y_, st);

    Face face;
    face.colour = colour;

    face.left.width = kEyeWidth;
    face.left.height = height_for(left_openness);
    face.left.radius = clamp(radius, 0, face.left.height / 2);
    face.left.centre_x = static_cast<int16_t>(kPanelWidth / 2 - kEyeCentreOffset + gaze_x);
    face.left.centre_y = static_cast<int16_t>(kEyeBaselineY + offset + gaze_y);

    face.right.width = kEyeWidth;
    face.right.height = height_for(openness);
    face.right.radius = clamp(radius, 0, face.right.height / 2);
    face.right.centre_x = static_cast<int16_t>(kPanelWidth / 2 + kEyeCentreOffset + gaze_x);
    face.right.centre_y = static_cast<int16_t>(kEyeBaselineY + offset + gaze_y);

    return face;
}

std::optional<Face> FaceAnimator::update(State state, uint32_t now_ms, double random_unit) {
    if (!started_) {
        started_ = true;
        state_ = state;
        from_ = to_ = expression_for(state);
        transition_started_ms_ = now_ms;
        next_blink_ms_ = now_ms + between(kBlinkMinGapMs, kBlinkMaxGapMs, random_unit);
        next_saccade_ms_ = now_ms + between(kSaccadeMinGapMs, kSaccadeMaxGapMs, random_unit);
        current_ = compose(now_ms);
        ++frames_;
        return current_;
    }

    if (state != state_) {
        // Ease from wherever the transition had reached, not from the old
        // resting shape. Restarting from rest makes a state change during a
        // transition jump backwards before moving forwards.
        const int32_t t = progress(transition_started_ms_, now_ms, kExpressionMs);
        Expression midpoint = to_;
        midpoint.openness = ease(from_.openness, to_.openness, t);
        midpoint.left_openness = ease(from_.left_openness, to_.left_openness, t);
        midpoint.vertical_offset = ease(from_.vertical_offset, to_.vertical_offset, t);
        midpoint.radius = ease(from_.radius, to_.radius, t);
        midpoint.colour = Rgb{
            static_cast<uint8_t>(ease(from_.colour.r, to_.colour.r, t)),
            static_cast<uint8_t>(ease(from_.colour.g, to_.colour.g, t)),
            static_cast<uint8_t>(ease(from_.colour.b, to_.colour.b, t)),
        };

        from_ = midpoint;
        to_ = expression_for(state);
        state_ = state;
        transition_started_ms_ = now_ms;
    }

    if (to_.animated) {
        if (blinking_ && now_ms - blink_started_ms_ >= kBlinkMs) {
            blinking_ = false;
            next_blink_ms_ = now_ms + between(kBlinkMinGapMs, kBlinkMaxGapMs, random_unit);
        }
        // Signed comparison, so the 49-day rollover of a millisecond counter
        // does not stop the face blinking for the rest of the uptime.
        if (!blinking_ && static_cast<int32_t>(now_ms - next_blink_ms_) >= 0) {
            blinking_ = true;
            blink_started_ms_ = now_ms;
        }

        if (static_cast<int32_t>(now_ms - next_saccade_ms_) >= 0) {
            gaze_from_x_ = gaze_to_x_;
            gaze_from_y_ = gaze_to_y_;
            gaze_to_x_ = static_cast<int16_t>(
                (static_cast<int>(between(0, 2 * kSaccadeRangeX + 1, random_unit)) -
                 kSaccadeRangeX));
            gaze_to_y_ = static_cast<int16_t>(
                (static_cast<int>(between(0, 2 * kSaccadeRangeY + 1, 1.0 - random_unit)) -
                 kSaccadeRangeY));
            saccade_started_ms_ = now_ms;
            next_saccade_ms_ = now_ms + between(kSaccadeMinGapMs, kSaccadeMaxGapMs, random_unit);
        }
    } else if (blinking_) {
        // Falling asleep mid-blink must not leave the lids stuck halfway.
        blinking_ = false;
    }

    const Face next = compose(now_ms);
    if (next == current_) {
        return std::nullopt;
    }
    current_ = next;
    ++frames_;
    return current_;
}

uint16_t isqrt(uint32_t value) {
    // Binary digit-by-digit method. Exact for every 32-bit input, no division
    // and no floating point.
    uint32_t remainder = value;
    uint32_t result = 0;
    uint32_t bit = 1u << 30;

    while (bit > remainder) {
        bit >>= 2;
    }
    while (bit != 0) {
        if (remainder >= result + bit) {
            remainder -= result + bit;
            result = (result >> 1) + bit;
        } else {
            result >>= 1;
        }
        bit >>= 2;
    }
    return static_cast<uint16_t>(result);
}

}  // namespace nova
