import Foundation
import Testing

@testable import NOVA

/// One banner per new alert, none for what was already there.
struct AlertTrackerTests {
    @Test("The same unacknowledged alerts on every poll are announced once")
    func announcesOnce() {
        var tracker = AlertTracker()
        let down = Alert.fixture(kind: .projectDown, secondsAgo: 60)

        #expect(tracker.unseen(in: [down]).map(\.id) == [down.id])
        #expect(tracker.unseen(in: [down]).isEmpty)
    }

    @Test("Priming swallows what was on the server at launch, and only that")
    func primingIsHistoryNotNews() {
        var tracker = AlertTracker()
        let old = Alert.fixture(kind: .projectDown, secondsAgo: 3600)
        tracker.prime(with: [old])

        let new = Alert.fixture(kind: .projectRecovered, secondsAgo: 5)
        #expect(tracker.unseen(in: [new, old]).map(\.id) == [new.id])
    }

    @Test("New alerts come back oldest first, so banners read in order")
    func orderedOldestFirst() {
        var tracker = AlertTracker()
        let later = Alert.fixture(kind: .projectRecovered, secondsAgo: 10)
        let earlier = Alert.fixture(kind: .projectDown, secondsAgo: 120)

        #expect(tracker.unseen(in: [later, earlier]).map(\.kind) == [.projectDown, .projectRecovered])
    }

    @Test("Decodes an alert page as the server writes it")
    func decodesPage() throws {
        let json = """
        {
          "items": [
            {
              "id": "b1a6d0ee-5a71-4d4c-9d6a-3a3a3f2a1c11",
              "kind": "project_down",
              "project": "Ghost",
              "message": "Ghost is not responding (1 check in a row).",
              "created_at": "2026-09-17T09:12:40.118214Z",
              "acknowledged_at": null
            }
          ],
          "unacknowledged": 1
        }
        """
        let page = try JSONCoding.decoder.decode(AlertPage.self, from: Data(json.utf8))
        #expect(page.unacknowledged == 1)
        #expect(page.items[0].kind == .projectDown)
        #expect(page.items[0].isDown)
        #expect(!page.items[0].isAcknowledged)
    }
}

extension Alert {
    static func fixture(kind: Kind, secondsAgo: TimeInterval) -> Alert {
        Alert(
            id: UUID(),
            kind: kind,
            project: "SnapWorth",
            message: kind == .projectDown ? "SnapWorth is not responding." : "SnapWorth is healthy again.",
            createdAt: Date(timeIntervalSinceNow: -secondsAgo),
            acknowledgedAt: nil
        )
    }
}
