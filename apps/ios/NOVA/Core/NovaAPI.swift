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
    func updateProfile(displayName: String) async throws -> User

    func devices() async throws -> [Device]
    func device(id: UUID) async throws -> Device
    func claimDevice(code: String, name: String?) async throws -> Device
    func renameDevice(id: UUID, name: String) async throws -> Device
    func removeDevice(id: UUID) async throws

    func conversations() async throws -> [Conversation]
    func conversation(id: UUID) async throws -> ConversationDetail
    func createConversation(title: String?) async throws -> Conversation
    func deleteConversation(id: UUID) async throws
    func sendMessage(_ content: String, to conversationID: UUID) async throws
        -> MessageExchange

    func telemetry(deviceID: UUID, limit: Int, eventType: String?) async throws
        -> [TelemetryEvent]
    func send(_ command: DeviceCommand, to deviceID: UUID) async throws -> CommandAccepted

    func memories(category: MemoryCategory?, limit: Int, offset: Int) async throws
        -> MemoryPage
    func searchMemories(_ query: String) async throws -> [MemorySearchResult]
    func updateMemory(id: UUID, _ update: UpdateMemoryRequest) async throws -> Memory
    func deleteMemory(id: UUID) async throws
    func forgetEverything() async throws
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

    func updateProfile(displayName: String) async throws -> User {
        try await client.send(
            Request(
                method: .patch,
                path: "users/me",
                body: UpdateProfileRequest(displayName: displayName)
            )
        )
    }

    // MARK: - Devices

    func devices() async throws -> [Device] {
        try await client.send(Request(path: "devices"))
    }

    func device(id: UUID) async throws -> Device {
        try await client.send(Request(path: "devices/\(id.uuidString.lowercased())"))
    }

    func claimDevice(code: String, name: String?) async throws -> Device {
        try await client.send(
            Request(
                method: .post,
                path: "devices/claim",
                body: ClaimDeviceRequest(code: code, name: name)
            )
        )
    }

    func renameDevice(id: UUID, name: String) async throws -> Device {
        try await client.send(
            Request(
                method: .patch,
                path: "devices/\(id.uuidString.lowercased())",
                body: RenameDeviceRequest(name: name)
            )
        )
    }

    func removeDevice(id: UUID) async throws {
        try await client.send(
            Request(method: .delete, path: "devices/\(id.uuidString.lowercased())")
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
    /// The streaming path is `ChatStreamClient`; this exists for callers
    /// that would rather have one response than parse an event stream.
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

    // MARK: - Telemetry and commands

    func telemetry(
        deviceID: UUID, limit: Int = 100, eventType: String? = nil
    ) async throws -> [TelemetryEvent] {
        var query = ["limit": String(limit)]
        if let eventType { query["event_type"] = eventType }

        return try await client.send(
            Request(path: "devices/\(deviceID.uuidString.lowercased())/telemetry", query: query)
        )
    }

    func send(
        _ command: DeviceCommand, to deviceID: UUID
    ) async throws -> CommandAccepted {
        try await client.send(
            Request(
                method: .post,
                path: "devices/\(deviceID.uuidString.lowercased())/commands",
                body: command
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
}
