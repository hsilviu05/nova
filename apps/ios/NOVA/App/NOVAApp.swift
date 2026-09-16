import SwiftUI

@main
struct NOVAApp: App {
    @State private var container = AppContainer()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(container.session)
                .environment(container.server)
                .environment(container.voice)
                .environment(\.novaAPI, container.api)
                .environment(\.chatStream, container.chatStream)
                // Rebuilt when the server address changes, which is what
                // makes "point this phone at the Mac mini instead" a setting
                // rather than a reinstall.
                .task(id: container.server.baseURL) {
                    await container.rebuildIfNeeded()
                }
                // And the tree is rebuilt with it, so no view is left
                // holding a client aimed at the previous machine.
                .id(container.server.baseURL)
        }
    }
}

/// Composition root.
///
/// Everything long-lived is built once, here, and handed down. Nothing
/// constructs its own dependencies, so swapping the API for a stub is a
/// change in this file rather than across every screen.
///
/// The clients are rebuilt when the server address changes. That is not free
/// -- it drops the connection pool and any in-flight request -- which is
/// correct: they were pointed at a different machine.
@MainActor
@Observable
final class AppContainer {
    let server: ServerSettings
    let tokens: TokenStore
    let voice: VoiceStore

    private(set) var client: APIClient
    private(set) var api: any NovaAPI
    private(set) var chatStream: ChatStreamClient
    private(set) var session: SessionStore

    /// The address the clients below were built for, so a change is noticed.
    private var builtFor: URL

    init(server: ServerSettings = ServerSettings()) {
        self.server = server
        let tokens = TokenStore()
        let configuration = server.configuration
        let client = APIClient(configuration: configuration, tokens: tokens)
        let api = LiveNovaAPI(client: client)

        self.tokens = tokens
        self.voice = VoiceStore()
        self.client = client
        self.api = api
        self.chatStream = ChatStreamClient(configuration: configuration, tokens: tokens)
        self.session = SessionStore(api: api, tokens: tokens, client: client)
        self.builtFor = configuration.baseURL
    }

    /// Point everything at the currently configured address.
    ///
    /// Called by `RootView` when it notices the address changed. The stored
    /// session is cleared: an account on one NOVA is not an account on
    /// another, and a token from one server will simply be rejected by the
    /// next, which is a confusing way to learn that.
    func rebuildIfNeeded() async {
        guard server.baseURL != builtFor else { return }

        await tokens.clear()

        let configuration = server.configuration
        let client = APIClient(configuration: configuration, tokens: tokens)
        let api = LiveNovaAPI(client: client)

        self.client = client
        self.api = api
        self.chatStream = ChatStreamClient(configuration: configuration, tokens: tokens)
        self.session = SessionStore(api: api, tokens: tokens, client: client)
        self.builtFor = configuration.baseURL
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
