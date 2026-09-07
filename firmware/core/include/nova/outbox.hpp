// The offline telemetry queue.
//
// NOVA's premise is that it keeps behaving when the network does not. That
// means telemetry produced during an outage has to go somewhere, and the
// device has a few hundred kilobytes of RAM and no disk worth the name.
//
// So: a bounded ring buffer that drops the *oldest* event when full.
//
// Dropping the oldest rather than refusing the newest is the whole design
// decision. A device that stops recording once its buffer fills spends a
// four-hour outage preserving the first ten minutes and losing the moment
// the network came back -- which is the part anyone debugging it wants. The
// recent past is worth more than the distant past, and the buffer should
// behave that way.
//
// Each event keeps its own `recorded_at` from the device clock, so a batch
// flushed on reconnect is not mistaken for a burst of live activity.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

#include "nova/protocol.hpp"

namespace nova {

// The protocol caps a batch at 100 events, so a flush never exceeds this.
inline constexpr size_t kMaxBatchSize = 100;

/// A bounded queue of telemetry awaiting delivery.
///
/// Capacity is a template parameter so the buffer is a member array rather
/// than a heap allocation: on a microcontroller, a queue that allocates
/// under memory pressure fails exactly when it is most needed.
template <size_t Capacity = 200>
class Outbox {
   public:
    static_assert(Capacity > 0, "an outbox with no capacity silently loses everything");

    /// Queue an event. Never fails; drops the oldest if full.
    void push(const TelemetryEvent &event) {
        if (count_ == Capacity) {
            // Full: advance the head, overwriting the oldest.
            head_ = (head_ + 1) % Capacity;
            --count_;
            ++dropped_;
        }
        const size_t tail = (head_ + count_) % Capacity;
        events_[tail] = event;
        ++count_;
    }

    /// The next batch to send, oldest first, without removing it.
    ///
    /// Peek rather than pop: the events must survive a send that fails. They
    /// are released only once the server has acknowledged them, so a
    /// connection that drops mid-flush loses nothing.
    std::vector<TelemetryEvent> peek(size_t limit = kMaxBatchSize) const {
        const size_t take = limit < count_ ? limit : count_;
        std::vector<TelemetryEvent> batch;
        batch.reserve(take);
        for (size_t index = 0; index < take; ++index) {
            batch.push_back(events_[(head_ + index) % Capacity]);
        }
        return batch;
    }

    /// Discard the oldest `n` events, after the server has taken them.
    void release(size_t n) {
        const size_t drop = n < count_ ? n : count_;
        head_ = (head_ + drop) % Capacity;
        count_ -= drop;
    }

    size_t size() const { return count_; }
    bool empty() const { return count_ == 0; }
    bool full() const { return count_ == Capacity; }
    static constexpr size_t capacity() { return Capacity; }

    /// How many events were lost to overflow.
    ///
    /// Reported in telemetry rather than kept quiet: a gap in the record is
    /// something the analytics phase must be able to see, or it will read an
    /// outage as an absence of activity.
    uint32_t dropped() const { return dropped_; }

    void clear() {
        head_ = 0;
        count_ = 0;
    }

   private:
    std::array<TelemetryEvent, Capacity> events_{};
    size_t head_ = 0;
    size_t count_ = 0;
    uint32_t dropped_ = 0;
};

}  // namespace nova
