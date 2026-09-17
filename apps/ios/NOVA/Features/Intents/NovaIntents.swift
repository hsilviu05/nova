import AppIntents
import Foundation

/// NOVA from Siri and Shortcuts, without opening the app.
///
/// "Is SnapWorth up?" from the lock screen is what a desk terminal is for,
/// and the answer already exists: it is the same status response the
/// dashboard renders. These intents ask for it and read one line of it out.
///
/// They run inside the app's own process, so they use the same stored
/// server address and the same Keychain session the app does. Nothing here
/// can change anything: the only endpoint reached is the read-only status,
/// and the tool system's confirmation gate is never in play.

/// A configured project, as something Siri can resolve from speech.
struct ProjectEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Project"
    static let defaultQuery = ProjectQuery()

    let id: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(id)")
    }
}

struct ProjectQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [ProjectEntity] {
        let known = await IntentSupport.projects()
        let wanted = Set(identifiers.map { $0.lowercased() })
        return known
            .filter { wanted.contains($0.name.lowercased()) }
            .map { ProjectEntity(id: $0.name) }
    }

    func suggestedEntities() async throws -> [ProjectEntity] {
        await IntentSupport.projects().map { ProjectEntity(id: $0.name) }
    }
}

/// "Is SnapWorth up?"
struct CheckProjectIntent: AppIntent {
    static let title: LocalizedStringResource = "Check a project"
    static let description: IntentDescription? = IntentDescription(
        "Asks NOVA whether one of your projects is healthy."
    )

    @Parameter(title: "Project")
    var project: ProjectEntity

    static var parameterSummary: some ParameterSummary {
        Summary("Check \(\.$project)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        guard let status = await IntentSupport.status() else {
            return .result(value: IntentSupport.notSignedIn, dialog: IntentDialog(stringLiteral: IntentSupport.notSignedIn))
        }
        guard let found = StatusPhrasing.match(project.id, in: status.projects) else {
            let line = "NOVA does not have a project called \(project.id)."
            return .result(value: line, dialog: IntentDialog(stringLiteral: line))
        }
        let line = StatusPhrasing.project(found)
        return .result(value: line, dialog: IntentDialog(stringLiteral: line))
    }
}

/// "How is NOVA?"
struct NovaStatusIntent: AppIntent {
    static let title: LocalizedStringResource = "NOVA status"
    static let description: IntentDescription? = IntentDescription(
        "One sentence on the model, the machine, and every project."
    )

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        guard let status = await IntentSupport.status() else {
            return .result(value: IntentSupport.notSignedIn, dialog: IntentDialog(stringLiteral: IntentSupport.notSignedIn))
        }
        let line = StatusPhrasing.overview(status)
        return .result(value: line, dialog: IntentDialog(stringLiteral: line))
    }
}

/// The phrases Siri listens for once the app is installed. No setup in the
/// Shortcuts app is needed for these.
struct NovaShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: CheckProjectIntent(),
            phrases: [
                "Is \(\.$project) up in \(.applicationName)",
                "Check \(\.$project) in \(.applicationName)",
                "Ask \(.applicationName) about \(\.$project)",
            ],
            shortTitle: "Check a project",
            systemImageName: "waveform.path.ecg"
        )
        AppShortcut(
            intent: NovaStatusIntent(),
            phrases: [
                "How is \(.applicationName)",
                "\(.applicationName) status",
            ],
            shortTitle: "NOVA status",
            systemImageName: "checkmark.circle"
        )
    }
}

/// What the intents share: a client built from the same stored address and
/// session the app uses, and the one request they make.
enum IntentSupport {
    static let notSignedIn = "Open NOVA and sign in first."

    /// The status, or nil when there is no session to ask with.
    ///
    /// A missing session is reported as a sentence rather than thrown: Siri
    /// renders a thrown error as a generic failure, and "sign in first" is
    /// the whole answer.
    static func status() async -> SystemStatus? {
        let tokens = TokenStore()
        guard await tokens.hasSession else { return nil }
        let configuration = await MainActor.run { ServerSettings().configuration }
        let api = LiveNovaAPI(client: APIClient(configuration: configuration, tokens: tokens))
        return try? await api.systemStatus()
    }

    static func projects() async -> [ProjectStatus] {
        await status()?.projects ?? []
    }
}
