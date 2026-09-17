import Foundation
import Testing

@testable import NOVA

/// What Siri says, checked as text: the intents themselves need the App
/// Intents runtime, but every word they speak comes from here.
struct IntentPhrasingTests {
    @Test("A healthy project reads out its latency and dependencies")
    func healthyProject() {
        let line = StatusPhrasing.project(
            .fixture(healthy: true, reachable: true, latencyMs: 8.4, dependencies: ["redis": true, "postgres": true])
        )
        #expect(line == "SnapWorth is healthy, answered in 8 ms. postgres ok, redis ok.")
    }

    @Test("A down dependency is named, not averaged away")
    func unhealthyDependency() {
        let line = StatusPhrasing.project(
            .fixture(healthy: false, reachable: true, statusCode: 503, dependencies: ["postgres": true, "redis": false])
        )
        #expect(line == "SnapWorth answered 503, which is not healthy. postgres ok, redis down.")
    }

    @Test("Unreachable is a sentence of its own, with no dependency list to mislead")
    func unreachable() {
        let line = StatusPhrasing.project(.fixture(healthy: false, reachable: false))
        #expect(line == "SnapWorth is not responding.")
    }

    @Test("An all-clear counts the projects")
    func overviewHealthy() {
        let status = SystemStatus.fixture(projects: [
            .fixture(name: "SnapWorth", healthy: true, reachable: true),
            .fixture(name: "Blog", healthy: true, reachable: true),
        ])
        #expect(StatusPhrasing.overview(status) == "All good. 2 of 2 projects are up.")
    }

    @Test("Problems are listed, and the model counts as one")
    func overviewProblems() {
        let status = SystemStatus.fixture(
            aiOnline: false,
            projects: [.fixture(name: "SnapWorth", healthy: false, reachable: false)]
        )
        #expect(StatusPhrasing.overview(status) == "2 problems: The model is not responding; SnapWorth is down.")
    }

    @Test("A spoken name matches case-insensitively, exact before prefix")
    func matching() {
        let projects: [ProjectStatus] = [
            .fixture(name: "api-v2", healthy: true, reachable: true),
            .fixture(name: "api", healthy: true, reachable: true),
            .fixture(name: "SnapWorth", healthy: true, reachable: true),
        ]
        #expect(StatusPhrasing.match("snap", in: projects)?.name == "SnapWorth")
        #expect(StatusPhrasing.match("API", in: projects)?.name == "api")
        #expect(StatusPhrasing.match("", in: projects) == nil)
        #expect(StatusPhrasing.match("nothing", in: projects) == nil)
    }
}

extension ProjectStatus {
    static func fixture(
        name: String = "SnapWorth",
        healthy: Bool,
        reachable: Bool,
        statusCode: Int? = nil,
        latencyMs: Double? = nil,
        dependencies: [String: Bool] = [:]
    ) -> ProjectStatus {
        ProjectStatus(
            name: name,
            healthy: healthy,
            reachable: reachable,
            description: nil,
            statusCode: statusCode,
            latencyMs: latencyMs,
            dependencies: dependencies
        )
    }
}

extension SystemStatus {
    static func fixture(aiOnline: Bool = true, projects: [ProjectStatus]) -> SystemStatus {
        SystemStatus(
            generatedAt: .now,
            ai: AIStatus(provider: "ollama", model: "qwen2.5:7b", online: aiOnline, supportsTools: true, latencyMs: nil, detail: nil),
            host: HostStatus(hostname: "mac", platform: "darwin", cpuCount: 8, loadPerCore: nil, memoryPercentUsed: nil, diskPercentUsed: nil),
            dependencies: [],
            projects: projects,
            memory: MemoryStatus(total: 0, recent: [], embeddingProvider: "lexical", stale: 0),
            tools: ToolsStatus(count: 0, groups: [], shellEnabled: false, invocationsToday: 0, failuresToday: 0),
            recentActivity: []
        )
    }
}
