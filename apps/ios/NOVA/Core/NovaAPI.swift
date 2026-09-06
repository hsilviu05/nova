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

    func telemetry(deviceID: UUID, limit: Int, eventType: String?) async throws
        -> [TelemetryEvent]
    func send(_ command: DeviceCommand, to deviceID: UUID) async throws -> CommandAccepted
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
}
