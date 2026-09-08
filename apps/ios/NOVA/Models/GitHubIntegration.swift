import Foundation

/// The owner's GitHub webhook, as the settings screen sees it.
///
/// There is no secret here. It exists in exactly one response -- the one
/// that creates or rotates the integration -- and never again, so a screen
/// that reloads cannot show it, and neither can a screenshot taken later.
struct GitHubIntegration: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    /// "owner/name", or nil to react to any repository the hook covers.
    let repository: String?
    let enabled: Bool
    /// Absolute when the server knows its public name; otherwise a path,
    /// and the screen says so.
    let webhookUrl: String
    let lastDeliveryAt: Date?
    /// e.g. "workflow_run:success". The answer to "is this wired up?".
    let lastEvent: String?
    let createdAt: Date

    var isAbsoluteURL: Bool { webhookUrl.hasPrefix("http") }
}

/// The creation response: the integration plus its secret, once.
struct GitHubIntegrationCreated: Codable, Equatable, Sendable {
    let id: UUID
    let repository: String?
    let enabled: Bool
    let webhookUrl: String
    let lastDeliveryAt: Date?
    let lastEvent: String?
    let createdAt: Date
    let secret: String
    let contentType: String

    var integration: GitHubIntegration {
        GitHubIntegration(
            id: id, repository: repository, enabled: enabled, webhookUrl: webhookUrl,
            lastDeliveryAt: lastDeliveryAt, lastEvent: lastEvent, createdAt: createdAt
        )
    }
}

struct CreateGitHubIntegrationRequest: Encodable, Sendable {
    var repository: String?
}

struct UpdateGitHubIntegrationRequest: Encodable, Sendable {
    var repository: String?
    var enabled: Bool?
}
