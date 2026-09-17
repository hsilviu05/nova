import Foundation

/// Turning a status response into one sentence Siri can say.
///
/// Kept apart from the intents so the wording is testable without the App
/// Intents runtime, and so the dashboard and Siri never disagree about what
/// "healthy" means -- both read the same `ProjectStatus`.
enum StatusPhrasing {
    /// One project, as an answer to "is X up?".
    static func project(_ project: ProjectStatus) -> String {
        guard project.reachable else {
            return "\(project.name) is not responding."
        }

        var sentence: String
        if project.healthy {
            sentence = "\(project.name) is healthy"
            if let latency = project.latencyMs {
                sentence += ", answered in \(Int(latency.rounded())) ms"
            }
        } else if let code = project.statusCode {
            sentence = "\(project.name) answered \(code), which is not healthy"
        } else {
            sentence = "\(project.name) is not healthy"
        }
        sentence += "."

        if !project.dependencies.isEmpty {
            let parts = project.dependencies
                .sorted { $0.key < $1.key }
                .map { "\($0.key) \($0.value ? "ok" : "down")" }
            sentence += " " + parts.joined(separator: ", ") + "."
        }
        return sentence
    }

    /// Everything at once, as an answer to "how is NOVA?".
    static func overview(_ status: SystemStatus) -> String {
        let projects = status.projects
        let healthy = projects.filter(\.healthy).count

        if status.isHealthy {
            var sentence = "All good."
            if status.ai.isOffline {
                sentence = "All good, though no model is attached."
            }
            if !projects.isEmpty {
                sentence += " \(healthy) of \(projects.count) "
                sentence += projects.count == 1 ? "project is up." : "projects are up."
            }
            return sentence
        }

        let problems = status.problems
        let lead = problems.count == 1 ? "One problem:" : "\(problems.count) problems:"
        return lead + " " + problems.joined(separator: "; ") + "."
    }

    /// The name a person said, matched against the projects NOVA knows.
    ///
    /// Case-insensitive, and a prefix is enough: "snap" finds "SnapWorth".
    /// An exact match wins over a prefix so "api" does not pick "api-v2"
    /// when both exist.
    static func match(_ spoken: String, in projects: [ProjectStatus]) -> ProjectStatus? {
        let wanted = spoken.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !wanted.isEmpty else { return nil }
        if let exact = projects.first(where: { $0.name.lowercased() == wanted }) {
            return exact
        }
        return projects.first { $0.name.lowercased().hasPrefix(wanted) }
    }
}
