// The face.
//
// Read this as a list of ways an animated face can be wrong: eyes that leave
// the panel, a blink that makes the device look crashed, a sleeping face that
// keeps blinking, a fixed blink rhythm that reads as machinery, and a still
// face redrawing the largest power draw on the board sixty times a second.

#include <set>
#include <vector>

#include "check.hpp"
#include "nova/face.hpp"

using namespace nova;

namespace {

/// Drive the animator for a span, returning every frame it emitted.
std::vector<Face> run(FaceAnimator &face, State state, uint32_t from_ms, uint32_t to_ms,
                      uint32_t step_ms = 16, double unit = 0.5) {
    std::vector<Face> frames;
    for (uint32_t t = from_ms; t <= to_ms; t += step_ms) {
        if (auto frame = face.update(state, t, unit)) {
            frames.push_back(*frame);
        }
    }
    return frames;
}

bool on_panel(const Eye &eye) {
    return eye.left() >= 0 && eye.right() <= kPanelWidth && eye.top() >= 0 &&
           eye.bottom() <= kPanelHeight;
}

void test_geometry() {
    CASE("the eyes never leave the panel, in any state") {
        // An eye clipped at the edge reads as a bug, not an expression, and
        // the panel driver would be asked to write outside its framebuffer.
        for (State state : {State::Idle, State::Listening, State::Thinking,
                            State::Speaking, State::Curious, State::Happy,
                            State::Confused, State::Alert, State::Sleeping,
                            State::Offline}) {
            FaceAnimator face;
            for (const Face &frame : run(face, state, 0, 12000, 13)) {
                CHECK(on_panel(frame.left));
                CHECK(on_panel(frame.right));
            }
        }
    }

    CASE("the eyes never overlap") {
        FaceAnimator face;
        for (const Face &frame : run(face, State::Idle, 0, 20000, 11)) {
            CHECK(frame.left.right() < frame.right.left());
        }
    }

    CASE("a shut eye is a line, not nothing") {
        // Height zero makes the face vanish, which looks like a crash rather
        // than a blink or sleep.
        FaceAnimator face;
        for (const Face &frame : run(face, State::Sleeping, 0, 5000, 17)) {
            CHECK(frame.left.height >= kEyeClosedHeight);
            CHECK(frame.right.height >= kEyeClosedHeight);
        }
    }

    CASE("the corner radius never exceeds half the height") {
        // A radius larger than the shape it rounds is undefined for any
        // sensible rasteriser, and squeezed-shut states are where it happens.
        FaceAnimator face;
        for (State state : {State::Happy, State::Sleeping, State::Thinking}) {
            for (const Face &frame : run(face, state, 0, 4000, 13)) {
                CHECK(frame.left.radius * 2 <= frame.left.height);
                CHECK(frame.right.radius * 2 <= frame.right.height);
            }
        }
    }

    CASE("the eyes sit above the vertical centre") {
        // Eyes exactly halfway down read as a mask rather than a face.
        FaceAnimator face;
        const Face frame = *face.update(State::Idle, 0, 0.5);
        CHECK(frame.left.centre_y < kPanelHeight / 2);
    }
}

void test_easing() {
    CASE("smoothstep is pinned at both ends") {
        CHECK_INT(smoothstep(0), 0);
        CHECK_INT(smoothstep(1000), 1000);
        CHECK_INT(smoothstep(500), 500);
    }

    CASE("smoothstep clamps outside its range") {
        CHECK_INT(smoothstep(-500), 0);
        CHECK_INT(smoothstep(5000), 1000);
    }

    CASE("smoothstep eases rather than running linear") {
        // The whole point: slow at the ends, fast in the middle. Linear
        // interpolation starts and stops abruptly, which nothing alive does.
        CHECK(smoothstep(100) < 100);
        CHECK(smoothstep(900) > 900);
    }

    CASE("smoothstep is monotonic") {
        int32_t previous = -1;
        for (int32_t t = 0; t <= 1000; ++t) {
            const int32_t value = smoothstep(t);
            CHECK(value >= previous);
            previous = value;
        }
    }

    CASE("the face itself moves on an eased curve") {
        // Testing smoothstep directly does not prove the face uses it. With
        // linear interpolation the first frames of a transition move as far
        // as the middle ones; eased, they move noticeably less.
        FaceAnimator face;
        face.update(State::Alert, 0, 0.5);
        const int16_t start = face.current().right.height;

        face.update(State::Happy, 1000, 0.5);
        const auto sample = [&](uint32_t offset) {
            face.update(State::Happy, 1000 + offset, 0.5);
            return start - face.current().right.height;
        };

        const int early = sample(kExpressionMs / 10);
        const int middle = sample(kExpressionMs / 2);
        const int total = sample(kExpressionMs);

        CHECK(total > 0);
        // Linear would put the tenth-way sample at about a tenth of the
        // total. Eased puts it far below that.
        CHECK(early * 10 < total);
        // And the midpoint still lands near half, which is what separates a
        // smoothstep from an arbitrary slow start.
        CHECK(middle * 2 > total - 4 && middle * 2 < total + 4);
    }

    CASE("ease reaches its endpoints exactly") {
        CHECK_INT(ease(10, 200, 0), 10);
        CHECK_INT(ease(10, 200, 1000), 200);
        CHECK_INT(ease(200, 10, 1000), 10);
    }
}

void test_expressions() {
    CASE("every state has a distinct resting face") {
        // Two states that look identical are two states the face cannot
        // communicate, which makes the behaviour engine's work invisible.
        std::set<std::tuple<int, int, int, int, int, int, int>> seen;
        for (State state : {State::Idle, State::Listening, State::Thinking,
                            State::Speaking, State::Curious, State::Happy,
                            State::Confused, State::Alert, State::Sleeping,
                            State::Offline}) {
            const Expression e = expression_for(state);
            seen.insert({e.openness, e.left_openness, e.vertical_offset, e.radius,
                         e.colour.r, e.colour.g, e.colour.b});
        }
        CHECK_INT(seen.size(), 10);
    }

    CASE("curiosity and confusion are asymmetric") {
        // A cocked head is one eye more open than the other. Symmetrical
        // eyes cannot express it at all.
        CHECK(expression_for(State::Curious).left_openness !=
              expression_for(State::Curious).openness);
        CHECK(expression_for(State::Confused).left_openness !=
              expression_for(State::Confused).openness);
    }

    CASE("idle is symmetric") {
        const Expression idle = expression_for(State::Idle);
        CHECK_INT(idle.left_openness, idle.openness);
    }

    CASE("sleeping is shut and not animated") {
        const Expression sleeping = expression_for(State::Sleeping);
        CHECK_INT(sleeping.openness, 0);
        CHECK(!sleeping.animated);
    }

    CASE("alert is the widest and squarest") {
        const Expression alert = expression_for(State::Alert);
        for (State state : {State::Idle, State::Thinking, State::Happy, State::Offline}) {
            CHECK(alert.openness >= expression_for(state).openness);
            CHECK(alert.radius <= expression_for(state).radius);
        }
    }
}

void test_blinking() {
    CASE("the face blinks") {
        FaceAnimator face;
        int16_t widest = 0;
        int16_t narrowest = 32767;
        for (const Face &frame : run(face, State::Idle, 0, 30000, 10)) {
            widest = frame.right.height > widest ? frame.right.height : widest;
            narrowest = frame.right.height < narrowest ? frame.right.height : narrowest;
        }
        // Something must have closed a long way at some point.
        CHECK(narrowest < widest / 2);
    }

    CASE("a blink reopens") {
        // A blink that sticks shut is worse than no blink at all.
        FaceAnimator face;
        const auto frames = run(face, State::Idle, 0, 30000, 10);
        CHECK(frames.size() > 2);
        const Face &last = frames.back();
        CHECK(last.right.height > kEyeHeight / 2);
    }

    CASE("blinks are not on a fixed rhythm") {
        // A metronomic blink is the clearest tell that something is a
        // machine; people notice without being able to say why. The property
        // is about the GAPS between blinks, so that is what is measured --
        // an earlier version of this case counted distinct eye heights, which
        // measures how smooth a blink is and says nothing about its rhythm.
        FaceAnimator face;
        std::vector<uint32_t> blink_starts;
        bool closing = false;

        for (uint32_t t = 0; t <= 120000; t += 10) {
            // A varying random source, the way a hardware RNG would behave.
            const double unit = static_cast<double>((t / 10) % 97) / 97.0;
            if (auto frame = face.update(State::Idle, t, unit)) {
                const bool shut = frame->right.height < kEyeHeight / 2;
                if (shut && !closing) {
                    blink_starts.push_back(t);
                }
                closing = shut;
            }
        }

        CHECK(blink_starts.size() > 8);

        std::set<uint32_t> gaps;
        for (size_t i = 1; i < blink_starts.size(); ++i) {
            gaps.insert(blink_starts[i] - blink_starts[i - 1]);
        }
        // A fixed schedule produces exactly one gap. Several means the
        // interval is genuinely being redrawn each time.
        CHECK(gaps.size() > 3);
    }

    CASE("every blink gap stays inside its documented bounds") {
        // Randomised, but not unbounded: a blink every twenty seconds reads
        // as a stare, and one every half second as a fault.
        FaceAnimator face;
        std::vector<uint32_t> blink_starts;
        bool closing = false;

        for (uint32_t t = 0; t <= 120000; t += 10) {
            const double unit = static_cast<double>((t / 10) % 97) / 97.0;
            if (auto frame = face.update(State::Idle, t, unit)) {
                const bool shut = frame->right.height < kEyeHeight / 2;
                if (shut && !closing) {
                    blink_starts.push_back(t);
                }
                closing = shut;
            }
        }

        for (size_t i = 1; i < blink_starts.size(); ++i) {
            const uint32_t gap = blink_starts[i] - blink_starts[i - 1];
            CHECK(gap >= kBlinkMinGapMs);
            CHECK(gap <= kBlinkMaxGapMs + kBlinkMs * 2);
        }
    }

    CASE("a blink is a movement, not a cut") {
        // The eye has to be caught partway shut. A blink that jumps from open
        // to closed in one frame is not perceived as a blink at all.
        FaceAnimator face;
        std::set<int16_t> heights;
        for (uint32_t t = 0; t <= 60000; t += 10) {
            const double unit = static_cast<double>((t / 10) % 97) / 97.0;
            if (auto frame = face.update(State::Idle, t, unit)) {
                heights.insert(frame->right.height);
            }
        }
        // Several intermediate heights between fully open and fully shut.
        int partway = 0;
        for (int16_t h : heights) {
            if (h > kEyeClosedHeight + 5 && h < kEyeHeight - 5) {
                ++partway;
            }
        }
        CHECK(partway >= 4);
    }

    CASE("a sleeping face does not blink or drift") {
        // Blinking while asleep is not asleep, and drifting the gaze behind
        // shut eyes burns the panel for something nobody can see.
        //
        // The random source has to VARY here. A constant 0.5 puts the saccade
        // target at exactly zero offset, so the face would sit still whether
        // or not animation was suppressed, and this case would pass against a
        // sleeping face that animates.
        FaceAnimator face;
        face.update(State::Sleeping, 0, 0.31);
        const Face settled = face.current();

        for (uint32_t t = 1000; t <= 60000; t += 10) {
            const double unit = static_cast<double>((t / 10) % 97) / 97.0;
            if (auto frame = face.update(State::Sleeping, t, unit)) {
                CHECK(*frame == settled);
            }
        }
        CHECK(face.current() == settled);
    }

    CASE("an awake face drifts its gaze") {
        // Eyes that hold one position perfectly read as a doll. This is the
        // other half of the case above: suppression while asleep only means
        // something if there is drift to suppress.
        FaceAnimator face;
        face.update(State::Idle, 0, 0.31);
        const int16_t settled_x = face.current().left.centre_x;

        bool moved = false;
        for (uint32_t t = 10; t <= 40000; t += 10) {
            const double unit = static_cast<double>((t / 10) % 97) / 97.0;
            if (auto frame = face.update(State::Idle, t, unit)) {
                if (frame->left.centre_x != settled_x) {
                    moved = true;
                }
            }
        }
        CHECK(moved);
    }

    CASE("gaze drift stays small") {
        // Drift is meant to read as life, not as the eyes wandering off. Both
        // eyes must also move together -- independently drifting eyes read as
        // a fault, not a glance.
        FaceAnimator face;
        face.update(State::Idle, 0, 0.31);
        const int16_t rest_left = face.current().left.centre_x;
        const int16_t rest_right = face.current().right.centre_x;

        for (uint32_t t = 10; t <= 40000; t += 10) {
            const double unit = static_cast<double>((t / 10) % 97) / 97.0;
            if (auto frame = face.update(State::Idle, t, unit)) {
                const int dx_left = frame->left.centre_x - rest_left;
                const int dx_right = frame->right.centre_x - rest_right;
                CHECK(dx_left <= kSaccadeRangeX && dx_left >= -kSaccadeRangeX);
                CHECK_INT(dx_left, dx_right);
            }
        }
    }
}

void test_power() {
    CASE("a settled face emits no frames") {
        // The panel is the largest draw on the board. A face that is not
        // moving must cost nothing, not sixty identical redraws a second.
        FaceAnimator face;
        face.update(State::Sleeping, 0, 0.5);
        const uint32_t after_first = face.frames();

        run(face, State::Sleeping, 100, 40000, 16);
        CHECK_INT(face.frames(), after_first);
    }

    CASE("an animated face emits far fewer frames than ticks") {
        // It should redraw when something changed, not on every tick.
        FaceAnimator face;
        uint32_t ticks = 0;
        for (uint32_t t = 0; t <= 30000; t += 10) {
            ++ticks;
            face.update(State::Idle, t, 0.5);
        }
        CHECK(face.frames() < ticks / 2);
        // But it must not be frozen either.
        CHECK(face.frames() > 10);
    }

    CASE("the first update always produces a frame") {
        // Nothing has been drawn yet, so there is no previous frame to match.
        FaceAnimator face;
        CHECK(face.update(State::Idle, 0, 0.5).has_value());
    }
}

void test_transitions() {
    CASE("a change of state moves the face gradually") {
        // A cut between expressions reads as a glitch. The face has to travel.
        FaceAnimator face;
        face.update(State::Idle, 0, 0.5);
        const int16_t before = face.current().right.height;

        const auto frames = run(face, State::Happy, 1000, 1000 + kExpressionMs, 16);
        CHECK(frames.size() > 3);

        const int16_t after = face.current().right.height;
        CHECK(after < before);
        // Intermediate frames must exist between the two resting shapes.
        bool intermediate = false;
        for (const Face &frame : frames) {
            if (frame.right.height < before && frame.right.height > after) {
                intermediate = true;
            }
        }
        CHECK(intermediate);
    }

    CASE("interrupting a transition does not jump backwards") {
        // Switching state mid-transition must continue from where the face
        // actually is, not restart from the shape it was leaving.
        FaceAnimator face;
        face.update(State::Idle, 0, 0.5);
        run(face, State::Happy, 10, 10 + kExpressionMs / 2, 16);
        const int16_t midway = face.current().right.height;

        const auto frames = run(face, State::Alert, 10 + kExpressionMs / 2,
                                10 + kExpressionMs / 2 + 40, 16);
        CHECK(!frames.empty());
        // Alert is more open than Happy, so the height must rise from where
        // it was -- never drop back towards the Idle shape first.
        for (const Face &frame : frames) {
            CHECK(frame.right.height >= midway - 2);
        }
    }

    CASE("colour transitions as well as shape") {
        FaceAnimator face;
        face.update(State::Idle, 0, 0.5);
        const Rgb idle = face.current().colour;

        run(face, State::Alert, 100, 100 + kExpressionMs, 16);
        CHECK(!(face.current().colour == idle));
    }
}

void test_rasteriser() {
    CASE("isqrt is exact") {
        for (uint32_t n = 0; n < 2000; ++n) {
            const uint16_t root = isqrt(n);
            CHECK(static_cast<uint32_t>(root) * root <= n);
            CHECK(static_cast<uint32_t>(root + 1) * (root + 1) > n);
        }
        CHECK_INT(isqrt(0), 0);
        CHECK_INT(isqrt(1u << 30), 1u << 15);
        CHECK_INT(isqrt(0xFFFFFFFFu), 65535);
    }

    CASE("a square-cornered eye is a plain rectangle") {
        Eye eye;
        eye.centre_x = 200;
        eye.centre_y = 200;
        eye.width = 40;
        eye.height = 20;
        eye.radius = 0;

        std::vector<Span> spans;
        rasterise(eye, [&](Span s) { spans.push_back(s); });

        CHECK_INT(spans.size(), 20);
        for (const Span &span : spans) {
            CHECK_INT(span.x0, 180);
            CHECK_INT(span.x1, 219);
            CHECK_INT(span.width(), 40);
        }
    }

    CASE("every span stays inside the eye") {
        // The panel driver writes these without bounds-checking them, so a
        // span outside the shape is a write outside the framebuffer.
        Eye eye;
        eye.centre_x = 120;
        eye.centre_y = 240;
        eye.width = 110;
        eye.height = 150;
        eye.radius = 34;

        int rows = 0;
        rasterise(eye, [&](Span s) {
            ++rows;
            CHECK(s.y >= eye.top());
            CHECK(s.y < eye.bottom());
            CHECK(s.x0 >= eye.left());
            CHECK(s.x1 < eye.right());
            CHECK(s.x1 >= s.x0);
        });
        CHECK_INT(rows, eye.height);
    }

    CASE("the corners are actually rounded") {
        // A rounded rect whose corner rows are full width is a rectangle with
        // a misleading name.
        Eye eye;
        eye.centre_x = 200;
        eye.centre_y = 200;
        eye.width = 100;
        eye.height = 100;
        eye.radius = 30;

        std::vector<Span> spans;
        rasterise(eye, [&](Span s) { spans.push_back(s); });

        CHECK(spans.size() > 2);
        // Narrow at the top, full in the middle, narrow again at the bottom.
        CHECK(spans.front().width() < spans[spans.size() / 2].width());
        CHECK(spans.back().width() < spans[spans.size() / 2].width());
        CHECK_INT(spans[spans.size() / 2].width(), eye.width);
    }

    CASE("the shape is symmetric top to bottom and left to right") {
        Eye eye;
        eye.centre_x = 205;
        eye.centre_y = 250;
        eye.width = 80;
        eye.height = 60;
        eye.radius = 20;

        std::vector<Span> spans;
        rasterise(eye, [&](Span s) { spans.push_back(s); });

        for (size_t i = 0; i < spans.size(); ++i) {
            const Span &mirror = spans[spans.size() - 1 - i];
            CHECK_INT(spans[i].width(), mirror.width());
            // Left and right insets must match, or the eye leans.
            CHECK_INT(spans[i].x0 - eye.left(), eye.right() - 1 - spans[i].x1);
        }
    }

    CASE("the width never decreases then increases within a half") {
        // Monotonic out to the middle. A wobble means the arc arithmetic is
        // wrong somewhere, which shows on the panel as a notched corner.
        Eye eye;
        eye.centre_x = 200;
        eye.centre_y = 200;
        eye.width = 110;
        eye.height = 150;
        eye.radius = 34;

        std::vector<Span> spans;
        rasterise(eye, [&](Span s) { spans.push_back(s); });

        for (size_t i = 1; i < spans.size() / 2; ++i) {
            CHECK(spans[i].width() >= spans[i - 1].width());
        }
    }

    CASE("a shut eye still rasterises") {
        // The sleeping face is a thin line with a radius clamped to match.
        // It must still produce spans, or NOVA disappears when it sleeps.
        FaceAnimator face;
        face.update(State::Sleeping, 0, 0.31);

        int rows = 0;
        rasterise(face.current().left, [&](Span s) {
            ++rows;
            CHECK(s.width() > 0);
        });
        CHECK(rows >= kEyeClosedHeight);
    }

    CASE("a radius larger than the eye does not invert it") {
        // Clamped internally rather than trusted: a caller-supplied radius
        // bigger than the half-height would drive the inset past the middle
        // and produce spans with x1 < x0.
        Eye eye;
        eye.centre_x = 200;
        eye.centre_y = 200;
        eye.width = 40;
        eye.height = 20;
        eye.radius = 500;

        int rows = 0;
        rasterise(eye, [&](Span s) {
            ++rows;
            CHECK(s.x1 >= s.x0);
        });
        CHECK(rows > 0);
    }
}

}  // namespace

int main() {
    std::printf("face\n");
    test_rasteriser();
    test_geometry();
    test_easing();
    test_expressions();
    test_blinking();
    test_power();
    test_transitions();
    return check::report("face");
}
