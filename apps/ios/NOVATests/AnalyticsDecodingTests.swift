import Foundation
import Testing

@testable import NOVA

/// Analytics payloads, captured verbatim from a running NOVA API.
///
/// Recorded rather than invented, for the same reason as the other decoding
/// suites: an invented fixture only proves the decoder agrees with whoever
/// wrote the fixture.
struct AnalyticsDecodingTests {
    /// A real `GET /api/v1/devices/{id}/analytics` response, with the hourly,
    /// daily and grid arrays trimmed to a few entries.
    private let analyticsJSON = """
    {
      "window_days": 30,
      "timezone": "Europe/Bucharest",
      "coverage": {
        "total_events": 201,
        "distinct_days": 21,
        "first_event_at": "2026-08-17T09:00:00Z",
        "last_event_at": "2026-09-06T18:41:19.516402Z",
        "is_sufficient": true
      },
      "presence_by_hour": [
        {"hour": 11, "count": 0},
        {"hour": 12, "count": 63},
        {"hour": 13, "count": 63},
        {"hour": 14, "count": 63}
      ],
      "presence_by_day": [
        {"day": "2026-08-17", "count": 9},
        {"day": "2026-08-18", "count": 9}
      ],
      "presence_by_weekday_hour": [
        {"weekday": 0, "hour": 12, "count": 9},
        {"weekday": 0, "hour": 13, "count": 9},
        {"weekday": 0, "hour": 14, "count": 9}
      ],
      "battery": [
        {"recorded_at": "2026-09-06T13:11:19.516402Z", "percent": 100},
        {"recorded_at": "2026-09-06T13:41:19.516402Z", "percent": 94},
        {"recorded_at": "2026-09-06T14:11:19.516402Z", "percent": 88}
      ],
      "event_types": [
        {"event_type": "person_detected", "count": 189},
        {"event_type": "heartbeat", "count": 12}
      ],
      "gaps": [
        {
          "started_at": "2026-09-06T13:11:19.516402Z",
          "ended_at": "2026-09-06T13:41:19.516402Z",
          "seconds": 1800
        }
      ]
    }
    """

    /// A real `GET /api/v1/devices/{id}/insights` response.
    private let insightsJSON = """
    {
      "window_days": 30,
      "timezone": "Europe/Bucharest",
      "coverage": {
        "total_events": 201,
        "distinct_days": 21,
        "first_event_at": "2026-08-17T09:00:00Z",
        "last_event_at": "2026-09-06T18:41:19.516402Z",
        "is_sufficient": true
      },
      "insights": [
        {
          "kind": "active_hours",
          "headline": "You're usually around between 12:00 and 15:00",
          "detail": "100% of the times NOVA noticed you fell in that window, across 21 days.",
          "confidence": "medium",
          "sample_size": 189,
          "days_observed": 21
        },
        {
          "kind": "battery_runtime",
          "headline": "About 2h 50m of battery left",
          "detail": "Dropping 12.0% an hour over the last 5h 30m, from 12 readings.",
          "confidence": "high",
          "sample_size": 12,
          "days_observed": 21
        }
      ],
      "insufficient_reason": null
    }
    """

    /// A real response from a device that has reported nothing.
    private let emptyInsightsJSON = """
    {
      "window_days": 30,
      "timezone": "UTC",
      "coverage": {
        "total_events": 0,
        "distinct_days": 0,
        "first_event_at": null,
        "last_event_at": null,
        "is_sufficient": false
      },
      "insights": [],
      "insufficient_reason": "NOVA hasn't reported anything yet."
    }
    """

    @Test("Decodes the analytics response")
    func analytics() throws {
        let analytics = try JSONCoding.decoder.decode(
            Analytics.self, from: Data(analyticsJSON.utf8)
        )

        #expect(analytics.windowDays == 30)
        #expect(analytics.timezone == "Europe/Bucharest")
        #expect(analytics.coverage.distinctDays == 21)
        #expect(analytics.coverage.isSufficient)
        #expect(analytics.presenceByHour.count == 4)
        #expect(analytics.battery.first?.percent == 100)
        #expect(analytics.gaps.first?.seconds == 1800)
    }

    @Test("Decodes a bare calendar date, not a timestamp")
    func dayBucketIsADate() throws {
        // presence_by_day carries "2026-08-17" with no time. A decoder set up
        // only for full ISO timestamps fails on it, and the failure looks
        // like the whole endpoint being broken.
        let analytics = try JSONCoding.decoder.decode(
            Analytics.self, from: Data(analyticsJSON.utf8)
        )
        let first = try #require(analytics.presenceByDay.first)

        let parts = Calendar(identifier: .gregorian).dateComponents(
            in: TimeZone(identifier: "UTC")!, from: first.day
        )
        #expect(parts.year == 2026)
        #expect(parts.month == 8)
        #expect(parts.day == 17)
        #expect(first.count == 9)
    }

    @Test("Decodes insights with their evidence")
    func insights() throws {
        let insights = try JSONCoding.decoder.decode(
            Insights.self, from: Data(insightsJSON.utf8)
        )

        #expect(insights.insufficientReason == nil)
        let first = try #require(insights.insights.first)
        #expect(first.kind == "active_hours")
        #expect(first.confidence == .medium)
        #expect(first.sampleSize == 189)
        #expect(first.daysObserved == 21)
        #expect(first.evidence == "189 events over 21 days")
    }

    @Test("An empty insight list carries a reason")
    func insufficient() throws {
        // An empty list *with* a reason is a normal response. Without one it
        // would be indistinguishable from a bug, which is why the server
        // always sets it.
        let insights = try JSONCoding.decoder.decode(
            Insights.self, from: Data(emptyInsightsJSON.utf8)
        )

        #expect(insights.insights.isEmpty)
        #expect(insights.insufficientReason == "NOVA hasn't reported anything yet.")
        #expect(!insights.coverage.isSufficient)
        #expect(insights.coverage.firstEventAt == nil)
    }

    @Test("Confidence reads as words, not as a raw enum")
    func confidenceLabels() {
        #expect(Insight.Confidence.low.label == "Tentative")
        #expect(Insight.Confidence.medium.label == "Fairly sure")
        #expect(Insight.Confidence.high.label == "Confident")
    }

    @Test("Evidence is singular for a single day")
    func singularEvidence() {
        let insight = Insight(
            kind: "active_hours",
            headline: "x",
            detail: "y",
            confidence: .low,
            sampleSize: 12,
            daysObserved: 1
        )
        #expect(insight.evidence == "12 events over 1 day")
    }

    @Test("Event type names read as English")
    func eventLabels() throws {
        let analytics = try JSONCoding.decoder.decode(
            Analytics.self, from: Data(analyticsJSON.utf8)
        )
        #expect(analytics.eventTypes.first?.label == "Person detected")
    }

    @Test("The heatmap scale never divides by zero")
    func peakIsNeverZero() throws {
        // A device with no telemetry returns an empty grid. Scaling opacity
        // by a peak of 0 produces NaN, and a NaN opacity is a blank chart
        // with no error anywhere.
        let empty = try JSONCoding.decoder.decode(
            Analytics.self,
            from: Data(
                analyticsJSON.replacingOccurrences(
                    of: """
                    [
                        {"weekday": 0, "hour": 12, "count": 9},
                        {"weekday": 0, "hour": 13, "count": 9},
                        {"weekday": 0, "hour": 14, "count": 9}
                      ]
                    """,
                    with: "[]"
                ).utf8
            )
        )
        #expect(empty.peakWeekdayHourCount >= 1)
    }

    @Test("Weekday 0 is Monday on both sides")
    func weekdayNaming() {
        // The server converts from Postgres's Sunday-first convention. If
        // the app disagreed, every row of the heatmap would be one day out
        // and still look plausible.
        let monday = WeekdayHourBucket(weekday: 0, hour: 9, count: 1)
        let sunday = WeekdayHourBucket(weekday: 6, hour: 9, count: 1)
        #expect(monday.weekdayName == "Mon")
        #expect(sunday.weekdayName == "Sun")
    }

    @Test("A user carries the timezone analytics are bucketed in")
    func userTimezone() throws {
        let json = """
        {
          "id": "d029d496-5b89-42b0-85c7-ead47b7b6d39",
          "email": "an@example.com",
          "display_name": "An",
          "timezone": "Europe/Bucharest",
          "is_active": true,
          "created_at": "2026-09-06T19:11:19.394815Z",
          "last_login_at": "2026-09-06T19:11:19.415612Z"
        }
        """
        let user = try JSONCoding.decoder.decode(User.self, from: Data(json.utf8))
        #expect(user.timezone == "Europe/Bucharest")
    }

    @Test("A profile update sends only what changed")
    func profileUpdateOmitsUnsetFields() throws {
        let update = UpdateProfileRequest(displayName: nil, timezone: "Europe/Bucharest")
        let encoded = try JSONCoding.encoder.encode(update)
        let object = try #require(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )

        #expect(object["timezone"] as? String == "Europe/Bucharest")
        // Sending display_name: null would blank the name server-side.
        #expect(object["display_name"] == nil)
    }
}
