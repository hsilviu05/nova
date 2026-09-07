// The reconnect policy.
//
// The failures worth testing here are the ones that only appear after the
// network has been down a long time -- an overflowed backoff that turns into
// a tight retry loop on attempt 22, or a rejected token retried forever
// because nothing distinguishes "wrong credentials" from "wifi is out".

#include "check.hpp"
#include "nova/connection.hpp"

using namespace nova;

namespace {

void test_backoff() {
    CASE("backoff doubles from the base") {
        CHECK_INT(ConnectionPolicy::base_delay_ms(0), kBackoffBaseMs);
        CHECK_INT(ConnectionPolicy::base_delay_ms(1), 2 * kBackoffBaseMs);
        CHECK_INT(ConnectionPolicy::base_delay_ms(2), 4 * kBackoffBaseMs);
        CHECK_INT(ConnectionPolicy::base_delay_ms(3), 8 * kBackoffBaseMs);
    }

    CASE("backoff is capped") {
        CHECK_INT(ConnectionPolicy::base_delay_ms(10), kBackoffMaxMs);
        CHECK_INT(ConnectionPolicy::base_delay_ms(100), kBackoffMaxMs);
    }

    CASE("backoff never overflows into a tight loop") {
        // A naive shift overflows 32 bits around attempt 22 and hands back a
        // few milliseconds -- a retry storm after the longest outage, which
        // is the worst possible moment for one.
        for (uint32_t attempts = 0; attempts < 200; ++attempts) {
            const uint32_t delay = ConnectionPolicy::base_delay_ms(attempts);
            CHECK(delay >= kBackoffBaseMs);
            CHECK(delay <= kBackoffMaxMs);
        }
    }

    CASE("backoff is monotonic") {
        uint32_t previous = 0;
        for (uint32_t attempts = 0; attempts < 40; ++attempts) {
            const uint32_t delay = ConnectionPolicy::base_delay_ms(attempts);
            CHECK(delay >= previous);
            previous = delay;
        }
    }
}

void test_jitter() {
    CASE("jitter stays within half and full of the backoff") {
        // The floor is the point: full jitter can return near-zero twice in a
        // row, and a device that is failing fast would then retry fast.
        ConnectionPolicy policy;
        for (int i = 0; i < 20; ++i) {
            const uint32_t base = ConnectionPolicy::base_delay_ms(policy.attempts());
            const double unit = static_cast<double>(i % 10) / 10.0;
            const Decision decision = policy.on_failure(Failure::Transient, unit);

            CHECK(decision.delay_ms >= base / 2);
            CHECK(decision.delay_ms <= base);
        }
    }

    CASE("different random draws give different delays") {
        ConnectionPolicy low;
        ConnectionPolicy high;
        // Advance both to the same attempt count so only the draw differs.
        low.on_failure(Failure::Transient, 0.0);
        high.on_failure(Failure::Transient, 0.0);

        const uint32_t a = low.on_failure(Failure::Transient, 0.0).delay_ms;
        const uint32_t b = high.on_failure(Failure::Transient, 0.99).delay_ms;
        CHECK(b > a);
    }

    CASE("a random value outside [0, 1) is clamped, not trusted") {
        ConnectionPolicy policy;
        const uint32_t base = ConnectionPolicy::base_delay_ms(0);

        ConnectionPolicy negative;
        CHECK(negative.on_failure(Failure::Transient, -5.0).delay_ms >= base / 2);

        CHECK(policy.on_failure(Failure::Transient, 42.0).delay_ms <= base);
    }
}

void test_reset() {
    CASE("a successful connection resets the backoff") {
        ConnectionPolicy policy;
        for (int i = 0; i < 6; ++i) {
            policy.on_failure(Failure::Transient, 0.5);
        }
        CHECK(policy.attempts() > 0);

        policy.on_connected();
        CHECK_INT(policy.attempts(), 0);

        // The next blip must not inherit a minute-long wait.
        const Decision decision = policy.on_failure(Failure::Transient, 0.0);
        CHECK_INT(decision.delay_ms, kBackoffBaseMs / 2);
    }

    CASE("a successful connection forgives earlier rejections") {
        // A token can be refused during a backend deploy. If those failures
        // persisted across a successful connection, a device would eventually
        // re-provision itself for no reason.
        ConnectionPolicy policy;
        policy.on_failure(Failure::Rejected, 0.5);
        policy.on_failure(Failure::Rejected, 0.5);
        policy.on_connected();
        CHECK_INT(policy.auth_failures(), 0);

        CHECK(policy.on_failure(Failure::Rejected, 0.5).disposition == Disposition::Retry);
    }
}

void test_reprovisioning() {
    CASE("repeated rejection stops the retry loop") {
        ConnectionPolicy policy;
        CHECK(policy.on_failure(Failure::Rejected, 0.5).disposition == Disposition::Retry);
        CHECK(policy.on_failure(Failure::Rejected, 0.5).disposition == Disposition::Retry);

        const Decision third = policy.on_failure(Failure::Rejected, 0.5);
        CHECK(third.disposition == Disposition::Reprovision);
        CHECK_INT(third.delay_ms, 0);
    }

    CASE("transient failures never trigger reprovisioning") {
        // Losing the wifi for a week must not make the device forget who it
        // belongs to.
        ConnectionPolicy policy;
        for (int i = 0; i < 500; ++i) {
            CHECK(policy.on_failure(Failure::Transient, 0.5).disposition == Disposition::Retry);
        }
        CHECK_INT(policy.auth_failures(), 0);
    }
}

void test_staleness() {
    CASE("silence past the timeout is treated as a dead connection") {
        // TCP does not notice a router that vanished; the socket stays open
        // and writes succeed into nothing.
        CHECK(!ConnectionPolicy::is_stale(0));
        CHECK(!ConnectionPolicy::is_stale(kSilenceTimeoutMs - 1));
        CHECK(ConnectionPolicy::is_stale(kSilenceTimeoutMs));
        CHECK(ConnectionPolicy::is_stale(kSilenceTimeoutMs * 10));
    }

    CASE("the timeout allows for more than one missed heartbeat") {
        // The server heartbeats every 30 s. Tearing the socket down on a
        // single missed beat would reconnect constantly on a slow link.
        CHECK(kSilenceTimeoutMs > 60000);
    }
}

}  // namespace

int main() {
    std::printf("connection\n");
    test_backoff();
    test_jitter();
    test_reset();
    test_reprovisioning();
    test_staleness();
    return check::report("connection");
}
