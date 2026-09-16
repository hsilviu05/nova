import Foundation

/// Everything the dashboard renders, in one response.
///
/// Mirrors `SystemStatus` in `nova.schemas.system`. Optional fields are
/// optional here for the same reason they are there: a NOVA with no projects
/// configured, no GitHub token and Docker turned off is correctly configured,
/// and its dashboard should say so rather than fail to decode.
struct SystemStatus: Decodable, Equatable, Sendable {
    let generatedAt: Date
    let ai: AIStatus
    let host: HostStatus
    let dependencies: [DependencyStatus]
    let projects: [ProjectStatus]
    let memory: MemoryStatus
    let tools: ToolsStatus
    let recentActivity: [ActivityEntry]

    /// One verdict for the status line at the top of the screen.
    ///
    /// Everything has to be right for this to be true, which is the point: a
    /// glance should distinguish "fine" from "look closer", and nothing else.
    var isHealthy: Bool {
        ai.online && dependencies.allSatisfy(\.healthy) && projects.allSatisfy(\.healthy)
    }

    var problems: [String] {
        var found: [String] = []
        if !ai.online { found.append(ai.detail ?? "The model is not responding") }
        found += dependencies.filter { !$0.healthy }.map { "\($0.name) is unreachable" }
        found += projects.filter { !$0.healthy }.map { "\($0.name) is down" }
        return found
    }
}

struct AIStatus: Decodable, Equatable, Sendable {
    let provider: String
    let model: String
    let online: Bool
    let supportsTools: Bool
    let latencyMs: Double?
    let detail: String?

    /// True when NOVA is answering with canned replies because no model is
    /// attached. Shown plainly, so nobody mistakes it for a working model.
    var isOffline: Bool { provider == "offline" }
}

struct HostStatus: Decodable, Equatable, Sendable {
    let hostname: String
    let platform: String
    let cpuCount: Int
    let loadPerCore: Double?
    let memoryPercentUsed: Double?
    let diskPercentUsed: Double?
}

struct DependencyStatus: Decodable, Equatable, Sendable, Identifiable {
    let name: String
    let healthy: Bool
    let latencyMs: Double?
    let error: String?

    var id: String { name }
}

struct ProjectStatus: Decodable, Equatable, Sendable, Identifiable {
    let name: String
    let healthy: Bool
    let reachable: Bool
    let description: String?
    let statusCode: Int?
    let latencyMs: Double?
    let dependencies: [String: Bool]

    var id: String { name }

    var summary: String {
        if !reachable { return "Not responding" }
        if healthy { return "Healthy" }
        return statusCode.map { "Answered \($0)" } ?? "Unhealthy"
    }
}

struct MemoryStatus: Decodable, Equatable, Sendable {
    let total: Int
    let recent: [String]
}

struct ToolsStatus: Decodable, Equatable, Sendable {
    let count: Int
    let groups: [String]
    let shellEnabled: Bool
    let invocationsToday: Int
    let failuresToday: Int
}

/// One line of the audit log.
struct ActivityEntry: Decodable, Equatable, Sendable, Identifiable {
    let id: UUID
    let toolName: String
    let toolGroup: String
    let permission: String
    let status: String
    let initiatedByModel: Bool
    let confirmed: Bool
    let durationMs: Int?
    let errorCode: String?
    let createdAt: Date

    var succeeded: Bool { status == "succeeded" }

    /// What to show next to the entry, in the person's words rather than the
    /// server's vocabulary.
    var outcome: String {
        switch status {
        case "succeeded": "ok"
        case "refused": "refused"
        case "timed_out": "timed out"
        default: "failed"
        }
    }
}

struct ActivityPage: Decodable, Equatable, Sendable {
    let items: [ActivityEntry]
}
