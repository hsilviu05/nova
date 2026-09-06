import Foundation

/// How much evidence the numbers rest on.
///
/// Carried on every analytics response and shown on screen. A chart without
/// it invites the reader to treat two days of data the same as two months.
struct Coverage: Codable, Equatable, Sendable {
    let totalEvents: Int
    let distinctDays: Int
    let firstEventAt: Date?
    let lastEventAt: Date?
    /// Whether there is enough here to say anything about habits.
    let isSufficient: Bool
}

/// One local hour of the day.
struct HourBucket: Codable, Identifiable, Equatable, Sendable {
    let hour: Int
    let count: Int

    var id: Int { hour }

    /// "09:00". Zero-padded so the axis labels line up.
    var label: String { String(format: "%02d:00", hour) }
}

struct DayBucket: Codable, Identifiable, Equatable, Sendable {
    let day: Date
    let count: Int

    var id: Date { day }
}

/// One cell of the 7×24 week grid. 0 is Monday, matching the server.
struct WeekdayHourBucket: Codable, Identifiable, Equatable, Sendable {
    let weekday: Int
    let hour: Int
    let count: Int

    var id: Int { weekday * 24 + hour }
}

struct BatteryPoint: Codable, Identifiable, Equatable, Sendable {
    let recordedAt: Date
    let percent: Int

    var id: Date { recordedAt }
}

struct EventTypeBucket: Codable, Identifiable, Equatable, Sendable {
    let eventType: String
    let count: Int

    var id: String { eventType }

    /// "person_detected" reads as "Person detected".
    var label: String {
        eventType.replacingOccurrences(of: "_", with: " ").capitalizedFirst
    }
}

/// A silence between heartbeats.
struct Gap: Codable, Identifiable, Equatable, Sendable {
    let startedAt: Date
    let endedAt: Date
    let seconds: Int

    var id: Date { startedAt }
}

/// Everything the insights screen plots.
struct Analytics: Codable, Equatable, Sendable {
    let windowDays: Int
    /// The timezone every bucket was computed in. Echoed by the server so a
    /// client rendering "14:00" knows whose two o'clock it is.
    let timezone: String
    let coverage: Coverage

    let presenceByHour: [HourBucket]
    let presenceByDay: [DayBucket]
    let presenceByWeekdayHour: [WeekdayHourBucket]
    let battery: [BatteryPoint]
    let eventTypes: [EventTypeBucket]
    let gaps: [Gap]

    /// The busiest cell, used to scale the heatmap. 1 rather than 0 when
    /// there is nothing, so nothing divides by zero.
    var peakWeekdayHourCount: Int {
        max(presenceByWeekdayHour.map(\.count).max() ?? 0, 1)
    }
}

/// One statement, with the evidence behind it.
struct Insight: Codable, Identifiable, Equatable, Sendable {
    enum Confidence: String, Codable, Sendable {
        case low, medium, high

        /// Shown next to the headline. A reader deciding whether to believe
        /// a claim about their own life deserves to see this.
        var label: String {
            switch self {
            case .low: "Tentative"
            case .medium: "Fairly sure"
            case .high: "Confident"
            }
        }
    }

    let kind: String
    let headline: String
    let detail: String
    let confidence: Confidence
    let sampleSize: Int
    let daysObserved: Int

    var id: String { kind }

    var symbol: String {
        switch kind {
        case "active_hours": "clock"
        case "busiest_weekday": "calendar"
        case "weekly_trend": "chart.line.uptrend.xyaxis"
        case "battery_runtime": "battery.50"
        case "connectivity": "wifi.exclamationmark"
        default: "sparkles"
        }
    }

    /// The support, spelled out. Deliberately not hidden behind a tap.
    var evidence: String {
        "\(sampleSize) events over \(daysObserved) day\(daysObserved == 1 ? "" : "s")"
    }
}

/// What can be said, and what cannot be said yet.
struct Insights: Codable, Equatable, Sendable {
    let windowDays: Int
    let timezone: String
    let coverage: Coverage
    let insights: [Insight]
    /// Set when NOVA declines to draw conclusions. An empty list *with* a
    /// reason is a normal response; an empty list without one would be
    /// indistinguishable from a bug.
    let insufficientReason: String?
}

private extension String {
    var capitalizedFirst: String {
        guard let first else { return self }
        return first.uppercased() + dropFirst()
    }
}
