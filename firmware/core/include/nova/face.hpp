// The face.
//
// NOVA's premise is a desk creature rather than a speaker with a screen, and
// the face is where that stands or falls (ADR 008). An AMOLED renders true
// black -- an unlit pixel emits nothing -- so eyes drawn on it float in the
// bezel instead of sitting on a visible rectangle. That is most of what makes
// this read as a face, and it is why the background is never drawn: not
// drawing it is both the correct look and free.
//
// Everything here is arithmetic. No panel, no framebuffer, no ESP-IDF. The
// animator takes a clock and returns geometry; `main/` turns geometry into
// two filled rounded rectangles. That split is deliberate -- "does NOVA look
// alive" becomes a property a host test can assert on, and the hardware layer
// shrinks to one drawing primitive.
//
// The expressive vocabulary is small on purpose: each eye has a position, a
// size and a corner radius, and that is all. No pupils, no slanted lids, no
// brows. Cozmo and Vector do more, but they blit whole frames; NOVA emits two
// rectangles. Openness, vertical offset, gaze and asymmetry between the two
// eyes turn out to carry every state the behaviour engine has, and a smaller
// vocabulary implemented well beats a larger one approximated.

#pragma once

#include <cstdint>
#include <optional>

#include "nova/behaviour.hpp"

namespace nova {

// The panel, from ADR 008.
inline constexpr int16_t kPanelWidth = 410;
inline constexpr int16_t kPanelHeight = 502;

// Eye geometry at rest.
inline constexpr int16_t kEyeWidth = 110;
inline constexpr int16_t kEyeHeight = 150;
inline constexpr int16_t kEyeGap = 70;
inline constexpr int16_t kEyeRadius = 34;

// Eyes sit above the vertical centre. A face with its eyes exactly halfway
// down reads as a mask; slightly high reads as a face looking at you.
inline constexpr int16_t kEyeBaselineY = 232;

/// A shut eye is a line, not an absence.
///
/// Collapsing the height to zero makes the face vanish mid-blink, which looks
/// like the device crashed rather than blinked.
inline constexpr int16_t kEyeClosedHeight = 10;

/// Colour, 8 bits per channel. Converted to the panel's format in `main/`,
/// because that is a property of the panel and not of the face.
struct Rgb {
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;

    constexpr bool operator==(const Rgb &other) const {
        return r == other.r && g == other.g && b == other.b;
    }
};

/// One eye: a filled rounded rectangle.
struct Eye {
    int16_t centre_x = 0;
    int16_t centre_y = 0;
    int16_t width = kEyeWidth;
    int16_t height = kEyeHeight;
    int16_t radius = kEyeRadius;

    constexpr int16_t left() const { return static_cast<int16_t>(centre_x - width / 2); }
    constexpr int16_t right() const { return static_cast<int16_t>(centre_x + width / 2); }
    constexpr int16_t top() const { return static_cast<int16_t>(centre_y - height / 2); }
    constexpr int16_t bottom() const { return static_cast<int16_t>(centre_y + height / 2); }

    constexpr bool operator==(const Eye &other) const {
        return centre_x == other.centre_x && centre_y == other.centre_y &&
               width == other.width && height == other.height && radius == other.radius;
    }
};

/// What to draw. Two eyes and a colour; the background is never drawn.
struct Face {
    Eye left;
    Eye right;
    Rgb colour;

    constexpr bool operator==(const Face &other) const {
        return left == other.left && right == other.right && colour == other.colour;
    }
};

/// The resting shape of a state, before blinking, gaze or easing.
///
/// Expressed as deltas from the neutral eye rather than absolute geometry, so
/// a change to the eye size does not require rewriting every expression.
struct Expression {
    /// Height as a fraction of kEyeHeight, in permille. 1000 is fully open.
    int16_t openness = 1000;
    /// Openness of the left eye only, when the two differ. A raised eyebrow
    /// is the cheapest way to draw curiosity, and asymmetry is what sells it.
    int16_t left_openness = 1000;
    /// Pixels the eyes sit above (negative) or below (positive) the baseline.
    int16_t vertical_offset = 0;
    /// Corner radius. Rounder reads softer; square reads alert.
    int16_t radius = kEyeRadius;
    Rgb colour{0, 220, 255};
    /// Whether the face is alive: blinking and idle drift. False while
    /// asleep, where any movement would be wrong.
    bool animated = true;
};

/// The resting expression for a behaviour state.
Expression expression_for(State state);

// -- animation timings ------------------------------------------------------
//
// Chosen for how a creature behaves, not for what is convenient to implement.

/// A blink, closing and opening. Human blinks are 100-150 ms; slower reads as
/// drowsy, faster is not perceived as a blink at all.
inline constexpr uint32_t kBlinkMs = 140;

/// The window between blinks. Randomised: a fixed interval is the single
/// clearest tell that something is a machine, and people notice it without
/// being able to say why.
inline constexpr uint32_t kBlinkMinGapMs = 2200;
inline constexpr uint32_t kBlinkMaxGapMs = 7000;

/// Idle gaze drift. Eyes that hold one position perfectly are unsettling --
/// the effect reads as a doll rather than as calm.
inline constexpr uint32_t kSaccadeMinGapMs = 1500;
inline constexpr uint32_t kSaccadeMaxGapMs = 4200;
inline constexpr uint32_t kSaccadeMs = 130;
inline constexpr int16_t kSaccadeRangeX = 14;
inline constexpr int16_t kSaccadeRangeY = 9;

/// How long a change of expression takes to land. Long enough to be seen as
/// movement rather than a cut, short enough not to feel sluggish.
inline constexpr uint32_t kExpressionMs = 260;

/// Smoothstep, in permille.
///
/// Linear interpolation between two expressions reads as mechanical: the
/// motion starts and stops abruptly, which nothing alive does. Easing in and
/// out at the ends is most of the difference between a robot face and a
/// creature's. Integer maths -- the ESP32-S3 has an FPU, but this runs every
/// frame and does not need one.
int32_t smoothstep(int32_t progress_permille);

/// Interpolate between two values by a permille amount, eased.
int16_t ease(int16_t from, int16_t to, int32_t progress_permille);

/// Drives the face over time.
///
/// Takes a clock and a unit random value rather than reading either, for the
/// same reason `ConnectionPolicy` does: a blink schedule that consults a
/// global generator cannot be tested, and one that reads a wall clock cannot
/// be tested quickly.
class FaceAnimator {
   public:
    /// Advance to `now_ms` and return the face if it changed.
    ///
    /// Nothing when the face is identical to the last frame it returned. That
    /// is the whole power story: the panel is the largest draw on the board,
    /// and a settled face costs no redraws at all rather than being redrawn
    /// sixty times a second with the same pixels.
    ///
    /// `random_unit` is in [0, 1) and is consumed only when a new blink or
    /// saccade has to be scheduled.
    std::optional<Face> update(State state, uint32_t now_ms, double random_unit);

    /// The current face, whether or not it has changed.
    Face current() const { return current_; }

    /// Frames actually emitted since boot.
    ///
    /// Exposed because a face redrawing far more often than it should is a
    /// power bug that is otherwise invisible -- the device just runs hot and
    /// flat. Not currently sent anywhere: TelemetryEvent has no field for it,
    /// and inventing one to carry a counter is not worth a protocol change.
    /// It is what a test asserts on, and what a bring-up log would print.
    uint32_t frames() const { return frames_; }

   private:
    Face compose(uint32_t now_ms) const;

    State state_ = State::Idle;
    Expression from_{};
    Expression to_{};
    uint32_t transition_started_ms_ = 0;

    uint32_t next_blink_ms_ = 0;
    uint32_t blink_started_ms_ = 0;
    bool blinking_ = false;

    uint32_t next_saccade_ms_ = 0;
    uint32_t saccade_started_ms_ = 0;
    int16_t gaze_from_x_ = 0, gaze_from_y_ = 0;
    int16_t gaze_to_x_ = 0, gaze_to_y_ = 0;

    Face current_{};
    bool started_ = false;
    uint32_t frames_ = 0;
};

/// One horizontal run of lit pixels: `y`, from `x0` to `x1` inclusive.
///
/// The panel is written as spans rather than pixels because that is what the
/// hardware is fast at -- one bounded write per row instead of a call per
/// pixel -- and because a span is a value a test can check without a
/// framebuffer to look at.
struct Span {
    int16_t y = 0;
    int16_t x0 = 0;
    int16_t x1 = 0;

    constexpr int16_t width() const { return static_cast<int16_t>(x1 - x0 + 1); }
};

/// Integer square root, for the corner arcs.
///
/// No floating point: this runs per row of every redrawn eye, and the ESP32's
/// FPU is not worth waking for a value that is about to be truncated anyway.
uint16_t isqrt(uint32_t value);

/// Emit the spans of one eye, top row first.
///
/// The corners are quarter-circles of `radius`. `emit` is called once per row
/// and never for an empty row, so a caller can write each span straight to the
/// panel without checking for degenerate ones.
template <typename Emit>
void rasterise(const Eye &eye, Emit emit) {
    const int16_t top = eye.top();
    const int16_t bottom = eye.bottom();
    const int16_t left = eye.left();
    const int16_t right = eye.right();
    const int16_t radius = clamp(eye.radius, 0, (bottom - top) / 2);

    for (int16_t y = top; y < bottom; ++y) {
        int16_t inset = 0;
        // Distance into the corner arc, measured from the row where the
        // straight edge begins. Rows between the two arcs have no inset.
        const int16_t from_top = static_cast<int16_t>(y - top);
        const int16_t from_bottom = static_cast<int16_t>(bottom - 1 - y);
        const int16_t depth = from_top < from_bottom ? from_top : from_bottom;

        if (depth < radius) {
            // x offset of a circle of `radius` at this height.
            const int32_t dy = radius - depth;
            const int32_t inside = static_cast<int32_t>(radius) * radius - dy * dy;
            inset = static_cast<int16_t>(radius - isqrt(static_cast<uint32_t>(
                                                       inside < 0 ? 0 : inside)));
        }

        const int16_t x0 = static_cast<int16_t>(left + inset);
        const int16_t x1 = static_cast<int16_t>(right - 1 - inset);
        if (x1 >= x0) {
            emit(Span{y, x0, x1});
        }
    }
}

}  // namespace nova
