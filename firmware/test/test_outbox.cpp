// The offline telemetry queue.
//
// The load-bearing claim is that a full outbox drops the *oldest* event and
// keeps recording. Get that backwards and a device that loses wifi for four
// hours preserves the first ten minutes and discards the moment the network
// came back -- which is the only part anyone was going to look at.

#include <string>

#include "check.hpp"
#include "nova/outbox.hpp"

using namespace nova;

namespace {

TelemetryEvent event(const std::string &recorded_at) {
    TelemetryEvent telemetry;
    telemetry.event_type = "presence.detected";
    telemetry.recorded_at = recorded_at;
    return telemetry;
}

void test_ordering() {
    CASE("an empty outbox yields nothing") {
        Outbox<4> outbox;
        CHECK(outbox.empty());
        CHECK_INT(outbox.size(), 0);
        CHECK_INT(outbox.peek().size(), 0);
        // Releasing from empty is a no-op rather than an underflow.
        outbox.release(10);
        CHECK_INT(outbox.size(), 0);
    }

    CASE("events come back oldest first") {
        Outbox<8> outbox;
        outbox.push(event("a"));
        outbox.push(event("b"));
        outbox.push(event("c"));

        const auto batch = outbox.peek();
        CHECK_INT(batch.size(), 3);
        CHECK_STR(batch[0].recorded_at.c_str(), "a");
        CHECK_STR(batch[2].recorded_at.c_str(), "c");
    }

    CASE("peek does not remove") {
        // A send can fail. If peek popped, a dropped connection mid-flush
        // would take the batch with it.
        Outbox<8> outbox;
        outbox.push(event("a"));
        outbox.push(event("b"));

        CHECK_INT(outbox.peek().size(), 2);
        CHECK_INT(outbox.peek().size(), 2);
        CHECK_INT(outbox.size(), 2);
    }

    CASE("release discards only what the server took") {
        Outbox<8> outbox;
        outbox.push(event("a"));
        outbox.push(event("b"));
        outbox.push(event("c"));

        outbox.release(2);
        CHECK_INT(outbox.size(), 1);
        CHECK_STR(outbox.peek()[0].recorded_at.c_str(), "c");
    }

    CASE("release beyond size empties rather than wrapping") {
        Outbox<4> outbox;
        outbox.push(event("a"));
        outbox.release(9);
        CHECK(outbox.empty());
        // The head must still be usable afterwards.
        outbox.push(event("b"));
        CHECK_STR(outbox.peek()[0].recorded_at.c_str(), "b");
    }

    CASE("peek honours the batch limit") {
        Outbox<8> outbox;
        for (int i = 0; i < 6; ++i) {
            outbox.push(event(std::to_string(i)));
        }
        const auto batch = outbox.peek(2);
        CHECK_INT(batch.size(), 2);
        CHECK_STR(batch[0].recorded_at.c_str(), "0");
        CHECK_STR(batch[1].recorded_at.c_str(), "1");
    }
}

void test_overflow() {
    CASE("a full outbox drops the oldest, not the newest") {
        Outbox<3> outbox;
        outbox.push(event("a"));
        outbox.push(event("b"));
        outbox.push(event("c"));
        CHECK(outbox.full());

        outbox.push(event("d"));

        const auto batch = outbox.peek();
        CHECK_INT(batch.size(), 3);
        CHECK_STR(batch[0].recorded_at.c_str(), "b");
        CHECK_STR(batch[2].recorded_at.c_str(), "d");
    }

    CASE("overflow is counted, not hidden") {
        // A gap in the record has to be visible downstream, or the analytics
        // phase reads an outage as an absence of activity.
        Outbox<2> outbox;
        CHECK_INT(outbox.dropped(), 0);

        for (int i = 0; i < 5; ++i) {
            outbox.push(event(std::to_string(i)));
        }
        CHECK_INT(outbox.dropped(), 3);
        CHECK_INT(outbox.size(), 2);
    }

    CASE("the ring survives many wraps") {
        // Exercises the modular arithmetic past the point where head and tail
        // have each lapped the buffer several times.
        Outbox<4> outbox;
        for (int i = 0; i < 100; ++i) {
            outbox.push(event(std::to_string(i)));
            if (i % 3 == 0) {
                outbox.release(1);
            }
        }
        const auto batch = outbox.peek();
        CHECK(batch.size() <= 4);
        CHECK_STR(batch.back().recorded_at.c_str(), "99");
        // Strictly increasing: no stale slot resurfaced.
        for (size_t i = 1; i < batch.size(); ++i) {
            CHECK(std::stoi(batch[i - 1].recorded_at) < std::stoi(batch[i].recorded_at));
        }
    }

    CASE("clear empties without resetting the drop count") {
        Outbox<2> outbox;
        for (int i = 0; i < 4; ++i) {
            outbox.push(event(std::to_string(i)));
        }
        outbox.clear();
        CHECK(outbox.empty());
        // The drops still happened; forgetting them would misreport the gap.
        CHECK_INT(outbox.dropped(), 2);
    }
}

void test_flush_cycle() {
    CASE("a failed send loses nothing and a retry sends the same batch") {
        Outbox<16> outbox;
        for (int i = 0; i < 5; ++i) {
            outbox.push(event(std::to_string(i)));
        }

        const auto attempt = outbox.peek(3);
        // Send fails: no release.
        const auto retry = outbox.peek(3);

        CHECK_INT(retry.size(), attempt.size());
        for (size_t i = 0; i < retry.size(); ++i) {
            CHECK_STR(retry[i].recorded_at.c_str(), attempt[i].recorded_at.c_str());
        }

        // Send succeeds.
        outbox.release(retry.size());
        CHECK_INT(outbox.size(), 2);
        CHECK_STR(outbox.peek()[0].recorded_at.c_str(), "3");
    }

    CASE("events queued during a flush are not lost") {
        Outbox<16> outbox;
        outbox.push(event("a"));
        outbox.push(event("b"));

        const auto in_flight = outbox.peek();
        // The device keeps observing while the send is in progress.
        outbox.push(event("c"));
        outbox.release(in_flight.size());

        CHECK_INT(outbox.size(), 1);
        CHECK_STR(outbox.peek()[0].recorded_at.c_str(), "c");
    }

    CASE("the default batch limit matches the protocol cap") {
        Outbox<256> outbox;
        for (int i = 0; i < 200; ++i) {
            outbox.push(event(std::to_string(i)));
        }
        CHECK_INT(outbox.peek().size(), kMaxBatchSize);
    }
}

}  // namespace

int main() {
    std::printf("outbox\n");
    test_ordering();
    test_overflow();
    test_flush_cycle();
    return check::report("outbox");
}
