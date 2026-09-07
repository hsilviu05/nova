#include "nova/connection.hpp"

namespace nova {

uint32_t ConnectionPolicy::base_delay_ms(uint32_t attempts) {
    if (attempts == 0) {
        return kBackoffBaseMs;
    }

    uint32_t delay = kBackoffBaseMs;
    for (uint32_t i = 0; i < attempts; ++i) {
        // Doubling with the cap checked *before* the multiply: at 32 bits,
        // attempt 22 would otherwise overflow and hand back a delay of a few
        // milliseconds, turning the backoff into a tight retry loop precisely
        // when the network has been down longest.
        if (delay >= kBackoffMaxMs / 2) {
            return kBackoffMaxMs;
        }
        delay *= 2;
    }
    return delay > kBackoffMaxMs ? kBackoffMaxMs : delay;
}

Decision ConnectionPolicy::on_failure(Failure failure, double random_unit) {
    if (failure == Failure::Rejected) {
        ++auth_failures_;
        if (auth_failures_ >= kAuthFailuresBeforeReprovision) {
            return Decision{Disposition::Reprovision, 0};
        }
    }

    const uint32_t base = base_delay_ms(attempts_);
    ++attempts_;

    // Equal jitter: half the backoff, plus a random share of the other half.
    //
    // Full jitter (a uniform draw over the whole interval) spreads a fleet
    // better but can return near-zero repeatedly, so a device that is failing
    // fast retries fast. Keeping half the delay as a floor bounds the worst
    // case while still breaking up the lockstep that follows a power cut.
    const double clamped = random_unit < 0.0 ? 0.0 : (random_unit >= 1.0 ? 0.999999 : random_unit);
    const uint32_t half = base / 2;
    const uint32_t jitter = static_cast<uint32_t>(static_cast<double>(half) * clamped);

    return Decision{Disposition::Retry, half + jitter};
}

}  // namespace nova
