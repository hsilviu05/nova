import SwiftUI

@main
struct NOVAApp: App {
    @State private var container = AppContainer()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(container.session)
                .environment(\.novaAPI, container.api)
                .environment(\.chatStream, container.chatStream)
        }
    }
}

/// Composition root.
///
/// Everything long-lived is built once, here, and handed down. Nothing
/// constructs its own dependencies, so swapping the API for a stub is a
/// change in this file rather than across every screen.
@MainActor
@Observable
final class AppContainer {
    let configuration: APIConfiguration
    let tokens: TokenStore
    let client: APIClient
    let api: any NovaAPI
    let chatStream: ChatStreamClient
    let session: SessionStore

    init(configuration: APIConfiguration = .fromEnvironment()) {
        self.configuration = configuration
        let tokens = TokenStore()
        let client = APIClient(configuration: configuration, tokens: tokens)
        let api = LiveNovaAPI(client: client)

        self.tokens = tokens
        self.client = client
        self.api = api
        self.chatStream = ChatStreamClient(configuration: configuration, tokens: tokens)
        self.session = SessionStore(api: api, tokens: tokens, client: client)
    }
}

extension APIConfiguration {
    /// Read the server address from the build settings, falling back to a
    /// local server.
    ///
    /// A launch argument override exists so UI tests can point the app at a
    /// stub without rebuilding.
    static func fromEnvironment() -> APIConfiguration {
        let arguments = ProcessInfo.processInfo.arguments
        if let index = arguments.firstIndex(of: "-nova-base-url"),
           index + 1 < arguments.count,
           let url = URL(string: arguments[index + 1]) {
            return APIConfiguration(baseURL: url)
        }

        if let raw = Bundle.main.object(forInfoDictionaryKey: "NOVABaseURL") as? String,
           !raw.isEmpty,
           let url = URL(string: raw) {
            return APIConfiguration(baseURL: url)
        }

        return .localDevelopment
    }
}

private struct NovaAPIKey: EnvironmentKey {
    static let defaultValue: any NovaAPI = LiveNovaAPI(
        client: APIClient(configuration: .localDevelopment, tokens: TokenStore())
    )
}

private struct ChatStreamKey: EnvironmentKey {
    static let defaultValue = ChatStreamClient(
        configuration: .localDevelopment, tokens: TokenStore()
    )
}

extension EnvironmentValues {
    var novaAPI: any NovaAPI {
        get { self[NovaAPIKey.self] }
        set { self[NovaAPIKey.self] = newValue }
    }

    var chatStream: ChatStreamClient {
        get { self[ChatStreamKey.self] }
        set { self[ChatStreamKey.self] = newValue }
    }
}
