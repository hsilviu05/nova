// When to reconnect, and when to stop trying.
//
// The device sits on a desk in a house whose wifi goes away for reasons
// nobody explains: a router reboot, a power cut, an ISP outage overnight. It
// has to come back on its own, and it has to do so without hammering a
// network that is already unwell.
//
// Two decisions live here, and neither of them belongs in the socket code:
//
//   * **How long to wait before the next attempt.** Exponential, capped, and
//     jittered. The jitter is not decoration -- a house losing power brings
//     every device back at the same instant, and a fleet that retries in
//     lockstep turns a router's slow boot into a sustained flood.
//
//   * **Whether waiting will help at all.** A refused authentication is not a
//     transient fault. Retrying a rejected token every minute forever burns
//     battery to no purpose and never tells the owner why their robot is
//     inert; the device should give up on the socket and go back to
//     advertising a claim code instead.
//
// No timers, no sockets, no ESP-IDF: this is arithmetic over a state, so the
// policy is testable and the I/O layer stays dumb.

#pragma once

#include <cstdint>

namespace nova {

/// First retry delay. Short enough that a router blip is invisible.
inline constexpr uint32_t kBackoffBaseMs = 1000;

/// Ceiling on the retry delay. A minute is the compromise between not
/// hammering a dead network and a device that appears broken because it is
/// sulking for ten minutes after the wifi came back.
inline constexpr uint32_t kBackoffMaxMs = 60000;

/// Consecutive authentication failures before the device stops retrying and
/// falls back to provisioning. Three rather than one: a token can be rejected
/// during a backend deploy, and re-provisioning is disruptive enough that it
/// should not be triggered by a thirty-second window.
inline constexpr uint32_t kAuthFailuresBeforeReprovision = 3;

/// A connection that has said nothing for this long is treated as dead.
///
/// TCP does not notice a router that vanished; the socket stays open and
/// writes succeed into a void. The server sends a heartbeat every 30 s, so
/// missing three in a row is the signal.
inline constexpr uint32_t kSilenceTimeoutMs = 100000;

/// Why an attempt ended.
enum class Failure : uint8_t {
    /// DNS, TCP, TLS, handshake -- anything that might work next time.
    Transient,
    /// The server rejected our credentials.
    Rejected,
};

/// What the device should do next.
enum class Disposition : uint8_t {
    /// Wait `delay_ms` and try again.
    Retry,
    /// Stop retrying. The credentials are not going to start working, so
    /// return to provisioning and show a claim code.
    Reprovision,
};

/// The next step after a failure.
struct Decision {
    Disposition disposition = Disposition::Retry;
    uint32_t delay_ms = kBackoffBaseMs;
};

/// The reconnect policy.
///
/// Deliberately not a clock consumer: it is handed the elapsed time it needs
/// and a unit random value, so every branch is reachable from a test without
/// waiting a real minute for a real backoff.
class ConnectionPolicy {
   public:
    /// Record a successful connection.
    ///
    /// Resets both counters. A connection that came up is evidence that the
    /// credentials are fine and the network is back, and carrying a stale
    /// backoff across it would make the *next* blip wait a minute for no
    /// reason.
    void on_connected() {
        attempts_ = 0;
        auth_failures_ = 0;
    }

    /// Record a failed attempt and decide what happens next.
    ///
    /// `random_unit` is a value in [0, 1) supplied by the caller -- on the
    /// device, from the hardware RNG. Passing it in rather than calling a
    /// generator keeps this deterministic under test, which is the only way
    /// to assert anything about jitter at all.
    Decision on_failure(Failure failure, double random_unit);

    /// Has the connection gone quiet long enough to be considered dead?
    ///
    /// Separate from `on_failure` because this is the failure mode that does
    /// not announce itself: nothing errors, the socket simply stops carrying
    /// anything.
    static bool is_stale(uint32_t since_last_frame_ms) {
        return since_last_frame_ms >= kSilenceTimeoutMs;
    }

    /// The unjittered backoff for a given attempt count, exposed for tests
    /// and for logging what the policy intended.
    static uint32_t base_delay_ms(uint32_t attempts);

    uint32_t attempts() const { return attempts_; }
    uint32_t auth_failures() const { return auth_failures_; }

   private:
    uint32_t attempts_ = 0;
    uint32_t auth_failures_ = 0;
};

}  // namespace nova
