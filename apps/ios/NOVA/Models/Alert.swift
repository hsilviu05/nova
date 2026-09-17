import Foundation

/// A project changing state while nobody was looking.
struct Alert: Decodable, Identifiable, Equatable, Sendable {
    enum Kind: String, Decodable, Sendable {
        case projectDown = "project_down"
        case projectRecovered = "project_recovered"
    }

    let id: UUID
    let kind: Kind
    let project: String
    let message: String
    let createdAt: Date
    let acknowledgedAt: Date?

    var isAcknowledged: Bool { acknowledgedAt != nil }
    var isDown: Bool { kind == .projectDown }
}

struct AlertPage: Decodable, Equatable, Sendable {
    let items: [Alert]
    /// Across every alert, not only this page.
    let unacknowledged: Int
}

/// The dashboard's summary of the watcher.
struct AlertsStatus: Decodable, Equatable, Sendable {
    let watching: Bool
    let intervalSeconds: Int
    let unacknowledged: Int
    let latest: Alert?
}

struct AcknowledgedCount: Decodable, Sendable {
    let acknowledged: Int
}
