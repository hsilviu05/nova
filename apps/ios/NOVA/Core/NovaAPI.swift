import Foundation

/// The API as the app uses it.
///
/// A protocol so views and models depend on the surface rather than on
/// `URLSession`, which is what makes previews and tests possible without a
/// server.
protocol NovaAPI: Sendable {
    func register(email: String, password: String, displayName: String) async throws
        -> AuthenticatedUser
    func signIn(email: String, password: String) async throws -> AuthenticatedUser
    func signOut(refreshToken: String) async throws

    func currentUser() async throws -> User
    func updateProfile(displayName: String?, timezone: String?) async throws -> User

    func conversations() async throws -> [Conversation]
    func conversation(id: UUID) async throws -> ConversationDetail
    func createConversation(title: String?) async throws -> Conversation
    func deleteConversation(id: UUID) async throws
    func sendMessage(_ content: String, to conversationID: UUID) async throws
        -> MessageExchange

    func memories(category: MemoryCategory?, limit: Int, offset: Int) async throws
        -> MemoryPage
    func searchMemories(_ query: String) async throws -> [MemorySearchResult]
    func updateMemory(id: UUID, _ update: UpdateMemoryRequest) async throws -> Memory
    func deleteMemory(id: UUID) async throws
    func forgetEverything() async throws

    func systemStatus() async throws -> SystemStatus
    func activity(limit: Int) async throws -> ActivityPage

    func tools() async throws -> ToolList
    func invokeTool(
        _ name: String, arguments: [String: String], confirmationToken: String?
    ) async throws -> ToolRunResult
}

/// `NovaAPI` over HTTP.
struct LiveNovaAPI: NovaAPI {
    private let client: APIClient

    init(client: APIClient) {
        self.client = client
    }

    // MARK: - Auth

    func register(
        email: String, password: String, displayName: String
    ) async throws -> AuthenticatedUser {
        try await client.send(
            Request(
                method: .post,
                path: "auth/register",
                body: RegisterRequest(
                    email: email, password: password, displayName: displayName
                ),
                requiresAuth: false
            )
        )
    }

    func signIn(email: String, password: String) async throws -> AuthenticatedUser {
        try await client.send(
            Request(
                method: .post,
                path: "auth/login",
                body: LoginRequest(email: email, password: password),
                requiresAuth: false
            )
        )
    }

    func signOut(refreshToken: String) async throws {
        try await client.send(
            Request(
                method: .post,
                path: "auth/logout",
                body: LogoutRequest(refreshToken: refreshToken),
                requiresAuth: false
            )
        )
    }

    // MARK: - User

    func currentUser() async throws -> User {
        try await client.send(Request(path: "users/me"))
    }

    /// Both fields are optional; omitted means unchanged.
    func updateProfile(
        displayName: String? = nil, timezone: String? = nil
    ) async throws -> User {
        try await client.send(
            Request(
                method: .patch,
                path: "users/me",
                body: UpdateProfileRequest(displayName: displayName, timezone: timezone)
            )
        )
    }

    // MARK: - Conversations

    func conversations() async throws -> [Conversation] {
        try await client.send(Request(path: "conversations"))
    }

    func conversation(id: UUID) async throws -> ConversationDetail {
        try await client.send(
            Request(path: "conversations/\(id.uuidString.lowercased())")
        )
    }

    func createConversation(title: String?) async throws -> Conversation {
        try await client.send(
            Request(
                method: .post,
                path: "conversations",
                body: CreateConversationRequest(title: title)
            )
        )
    }

    func deleteConversation(id: UUID) async throws {
        try await client.send(
            Request(
                method: .delete, path: "conversations/\(id.uuidString.lowercased())"
            )
        )
    }

    /// Send and wait for the whole reply.
    ///
    /// The streaming path is `ChatStreamClient`, and it is the one the app
    /// actually uses: tools only run there, because tool use is something a
    /// person watches happen. This exists for callers with nobody watching.
    func sendMessage(
        _ content: String, to conversationID: UUID
    ) async throws -> MessageExchange {
        try await client.send(
            Request(
                method: .post,
                path: "conversations/\(conversationID.uuidString.lowercased())/messages",
                body: SendMessageRequest(content: content)
            )
        )
    }

    // MARK: - Memory

    func memories(
        category: MemoryCategory? = nil, limit: Int = 50, offset: Int = 0
    ) async throws -> MemoryPage {
        var query = ["limit": String(limit), "offset": String(offset)]
        if let category, category != .unknown {
            query["category"] = category.rawValue
        }

        return try await client.send(Request(path: "memories", query: query))
    }

    func searchMemories(_ query: String) async throws -> [MemorySearchResult] {
        try await client.send(Request(path: "memories/search", query: ["q": query]))
    }

    func updateMemory(id: UUID, _ update: UpdateMemoryRequest) async throws -> Memory {
        try await client.send(
            Request(
                method: .patch,
                path: "memories/\(id.uuidString.lowercased())",
                body: update
            )
        )
    }

    func deleteMemory(id: UUID) async throws {
        try await client.send(
            Request(method: .delete, path: "memories/\(id.uuidString.lowercased())")
        )
    }

    /// Clear everything NOVA has learned, without deleting the account.
    func forgetEverything() async throws {
        try await client.send(Request(method: .delete, path: "memories"))
    }

    // MARK: - System

    /// Everything the dashboard shows, in one request.
    ///
    /// A longer timeout than the default: this probes the model provider and
    /// every configured project, and on a cold Ollama the first token can
    /// take a few seconds. Still bounded, because a dashboard that hangs is
    /// worse than one that says it could not reach something.
    func systemStatus() async throws -> SystemStatus {
        try await client.send(Request(path: "system/status", timeout: 30))
    }

    func activity(limit: Int = 50) async throws -> ActivityPage {
        try await client.send(
            Request(path: "system/activity", query: ["limit": String(limit)])
        )
    }

    // MARK: - Tools

    func tools() async throws -> ToolList {
        try await client.send(Request(path: "tools"))
    }

    /// Run a tool.
    ///
    /// Without a token, a destructive tool answers 409 and the error carries
    /// the prompt to show and the token to send back. That round trip is the
    /// confirmation: there is no way to skip it from here, because there is
    /// no other endpoint.
    func invokeTool(
        _ name: String,
        arguments: [String: String],
        confirmationToken: String? = nil
    ) async throws -> ToolRunResult {
        try await client.send(
            Request(
                method: .post,
                path: "tools/invoke",
                body: InvokeToolRequest(
                    name: name,
                    arguments: arguments,
                    confirmationToken: confirmationToken
                ),
                // Tools spawn processes and reach other services. The
                // server's own budget is shorter than this; the margin is so
                // a tool that times out reports as a timeout rather than as
                // the phone giving up first.
                timeout: 45
            )
        )
    }
}
