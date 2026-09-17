import Foundation
import Testing

@testable import NOVA

/// Who is signed in, and what it takes to stop being.
///
/// Two properties here are security properties rather than conveniences:
///
/// * Signing out clears the device's credentials whether or not the server
///   could be told. A person who taps "sign out" on a train has left the
///   session; leaving the refresh token on the phone because the request
///   timed out would mean they had not.
/// * A stored token is not assumed valid at launch. The server is asked. That
///   is what makes a revoked session take effect when the app is next opened
///   rather than whenever the access token happens to expire.
@MainActor
struct SessionStoreTests {
    // MARK: - A fake API, scripted per test

    /// A `NovaAPI` scripted per test.
    ///
    /// Only the five calls `SessionStore` makes are implemented. Every other
    /// member of the protocol throws rather than inventing an answer, so a
    /// test that starts depending on one says so instead of quietly passing.
    final class FakeAPI: NovaAPI, @unchecked Sendable {
        enum Behaviour: Sendable {
            case succeeds
            case rejects(APIError)
        }

        nonisolated(unsafe) var currentUserBehaviour: Behaviour = .succeeds
        nonisolated(unsafe) var signOutBehaviour: Behaviour = .succeeds
        nonisolated(unsafe) var authenticateBehaviour: Behaviour = .succeeds
        nonisolated(unsafe) private(set) var signedOutWith: [String] = []
        nonisolated(unsafe) private(set) var currentUserCalls = 0
        /// How many `currentUser` calls reject before the rest succeed.
        /// Used to model an access token that has aged out: the first call
        /// fails, and the one after the renewal does not.
        nonisolated(unsafe) var currentUserFailuresRemaining = 0

        static let user = User(
            id: UUID(),
            email: "someone@example.com",
            displayName: "Someone",
            timezone: "Europe/Bucharest",
            isActive: true,
            createdAt: .now,
            lastLoginAt: nil
        )

        private static let pair = TokenPair(
            accessToken: "access-new",
            refreshToken: "refresh-new",
            tokenType: "bearer",
            expiresIn: 900
        )

        nonisolated func currentUser() async throws -> User {
            currentUserCalls += 1
            if currentUserFailuresRemaining > 0 {
                currentUserFailuresRemaining -= 1
                throw APIError.api(
                    status: 401,
                    envelope: APIErrorEnvelope.Detail(
                        code: "token_expired",
                        message: "Access token has expired.",
                        requestId: nil,
                        details: nil
                    )
                )
            }
            if case let .rejects(error) = currentUserBehaviour { throw error }
            return Self.user
        }

        nonisolated func signOut(refreshToken: String) async throws {
            signedOutWith.append(refreshToken)
            if case let .rejects(error) = signOutBehaviour { throw error }
        }

        nonisolated func signIn(email: String, password: String) async throws -> AuthenticatedUser {
            if case let .rejects(error) = authenticateBehaviour { throw error }
            return AuthenticatedUser(user: Self.user, tokens: Self.pair)
        }

        nonisolated func register(
            email: String, password: String, displayName: String
        ) async throws -> AuthenticatedUser {
            if case let .rejects(error) = authenticateBehaviour { throw error }
            return AuthenticatedUser(user: Self.user, tokens: Self.pair)
        }

        nonisolated func updateProfile(
            displayName: String?, timezone: String?
        ) async throws -> User {
            if case let .rejects(error) = currentUserBehaviour { throw error }
            return User(
                id: Self.user.id,
                email: Self.user.email,
                displayName: displayName ?? Self.user.displayName,
                timezone: timezone ?? Self.user.timezone,
                isActive: true,
                createdAt: Self.user.createdAt,
                lastLoginAt: nil
            )
        }

        // Not reached from SessionStore.
        nonisolated func conversations() async throws -> [Conversation] { throw APIError.unauthenticated }
        nonisolated func searchConversations(_ query: String) async throws -> [ConversationSearchResult] { throw APIError.unauthenticated }
        nonisolated func conversation(id: UUID) async throws -> ConversationDetail { throw APIError.unauthenticated }
        nonisolated func createConversation(title: String?) async throws -> Conversation { throw APIError.unauthenticated }
        nonisolated func deleteConversation(id: UUID) async throws { throw APIError.unauthenticated }
        nonisolated func sendMessage(_ content: String, to conversationID: UUID) async throws -> MessageExchange { throw APIError.unauthenticated }
        nonisolated func memories(category: MemoryCategory?, limit: Int, offset: Int) async throws -> MemoryPage { throw APIError.unauthenticated }
        nonisolated func searchMemories(_ query: String) async throws -> [MemorySearchResult] { throw APIError.unauthenticated }
        nonisolated func updateMemory(id: UUID, _ update: UpdateMemoryRequest) async throws -> Memory { throw APIError.unauthenticated }
        nonisolated func deleteMemory(id: UUID) async throws { throw APIError.unauthenticated }
        nonisolated func forgetEverything() async throws { throw APIError.unauthenticated }
        nonisolated func systemStatus() async throws -> SystemStatus { throw APIError.unauthenticated }
        nonisolated func alerts(unacknowledgedOnly: Bool) async throws -> AlertPage { AlertPage(items: [], unacknowledged: 0) }
        nonisolated func acknowledgeAlert(id: UUID) async throws -> Alert { throw APIError.unauthenticated }
        nonisolated func acknowledgeAllAlerts() async throws -> AcknowledgedCount { AcknowledgedCount(acknowledged: 0) }
        nonisolated func activity(limit: Int) async throws -> ActivityPage { throw APIError.unauthenticated }
        nonisolated func tools() async throws -> ToolList { throw APIError.unauthenticated }
        nonisolated func invokeTool(_ name: String, arguments: [String: String], confirmationToken: String?) async throws -> ToolRunResult { throw APIError.unauthenticated }
        nonisolated func githubIntegration() async throws -> GitHubIntegration { throw APIError.unauthenticated }
        nonisolated func connectGitHub(repository: String?) async throws -> GitHubIntegrationCreated { throw APIError.unauthenticated }
        nonisolated func updateGitHubIntegration(repository: String?, enabled: Bool?) async throws -> GitHubIntegration { throw APIError.unauthenticated }
        nonisolated func disconnectGitHub() async throws { throw APIError.unauthenticated }
    }

    private func environment(
        stored: TokenPair? = nil
    ) async -> (SessionStore, FakeAPI, TokenStore, StubTransport.Script) {
        let tokens = TokenStore(service: "com.nova.app.tests.\(UUID().uuidString)")
        if let stored { await tokens.save(stored) }

        let (session, script) = StubTransport.session()
        let client = APIClient(
            configuration: APIConfiguration(baseURL: URL(string: "http://192.168.1.20:8000")!),
            tokens: tokens,
            session: session
        )
        let api = FakeAPI()
        return (SessionStore(api: api, tokens: tokens, client: client), api, tokens, script)
    }

    private var storedPair: TokenPair {
        TokenPair(
            accessToken: "access-1",
            refreshToken: "refresh-1",
            tokenType: "bearer",
            expiresIn: 900
        )
    }

    private func unauthorised() -> APIError {
        .api(
            status: 401,
            envelope: APIErrorEnvelope.Detail(
                code: "token_invalid",
                message: "Access token is invalid.",
                requestId: nil,
                details: nil
            )
        )
    }

    // MARK: - Launch

    @Test("With nothing stored, the app goes straight to signed out")
    func noStoredSession() async {
        let (store, api, _, _) = await environment()

        await store.restore()

        #expect(store.state == .signedOut)
        // And the server is not asked, because there is nothing to ask about.
        #expect(api.currentUserCalls == 0)
    }

    @Test("A stored session is checked with the server, not assumed")
    func storedSessionIsVerified() async {
        // This is what makes a deactivated account or a revoked session take
        // effect at launch rather than whenever the access token ages out.
        let (store, api, _, _) = await environment(stored: storedPair)

        await store.restore()

        #expect(api.currentUserCalls == 1)
        #expect(store.user?.email == "someone@example.com")
    }

    @Test("An access token that aged out overnight is renewed rather than dropped")
    func expiredAccessTokenIsRenewed() async {
        // The ordinary path for an app opened the next morning. Signing the
        // person out here would make a fifteen-minute access token mean a
        // fifteen-minute session.
        let (store, api, tokens, script) = await environment(stored: storedPair)
        script.respond(
            json: #"{"access_token":"access-2","refresh_token":"refresh-2","token_type":"bearer","expires_in":900}"#
        )

        // Fails once, the way an expired access token does, then succeeds
        // with the renewed one.
        api.currentUserFailuresRemaining = 1

        await store.restore()

        #expect(store.user != nil)
        #expect(api.currentUserCalls == 2)
        #expect(await tokens.accessToken == "access-2")
    }

    @Test("A session the server no longer accepts is cleared from the device")
    func revokedSessionIsCleared() async {
        // Leaving a dead refresh token on the phone would mean every launch
        // repeats a request that cannot succeed.
        let (store, api, tokens, script) = await environment(stored: storedPair)
        api.currentUserBehaviour = .rejects(unauthorised())
        script.respond(
            status: 401,
            json: #"{"error":{"code":"token_reused","message":"Session revoked.","request_id":null,"details":null}}"#
        )

        await store.restore()

        #expect(store.state == .signedOut)
        #expect(await !tokens.hasSession)
    }

    // MARK: - Signing out

    @Test("Signing out tells the server which family to revoke")
    func signOutRevokesServerSide() async {
        // Clearing only the device would leave the refresh token live on the
        // server until it expired -- and NOVA's rotate rather than expire, so
        // that is indefinitely.
        let (store, api, tokens, _) = await environment(stored: storedPair)
        await store.restore()

        await store.signOut()

        #expect(api.signedOutWith == ["refresh-1"])
        #expect(await !tokens.hasSession)
        #expect(store.state == .signedOut)
    }

    @Test("Signing out clears the device even when the server cannot be told")
    func signOutIsLocalFirst() async {
        // A person on a train taps sign out. The request fails. They have
        // still left the session: keeping their credentials because the
        // network was down is not a defensible reading of what they asked
        // for.
        let (store, api, tokens, _) = await environment(stored: storedPair)
        api.signOutBehaviour = .rejects(.transport(URLError(.notConnectedToInternet)))
        await store.restore()

        await store.signOut()

        #expect(store.state == .signedOut)
        #expect(await !tokens.hasSession)
        #expect(await tokens.refreshToken == nil)
    }

    @Test("Signing out with nothing stored still ends in signed out")
    func signOutWithoutASession() async {
        let (store, api, tokens, _) = await environment()

        await store.signOut()

        #expect(store.state == .signedOut)
        #expect(await !tokens.hasSession)
        // Nothing to revoke, so nothing was sent.
        #expect(api.signedOutWith.isEmpty)
    }

    @Test("Signing out clears the error left over from a failed attempt")
    func signOutClearsTheError() async {
        // Otherwise the sign-in screen appears with the previous failure
        // already on it, which reads as the sign-out having failed.
        let (store, api, _, _) = await environment()
        api.authenticateBehaviour = .rejects(unauthorised())
        await store.signIn(email: "a@example.com", password: "wrong-password")
        #expect(store.error != nil)

        await store.signOut()

        #expect(store.error == nil)
    }

    // MARK: - Signing in

    @Test("A successful sign-in stores the pair and the user")
    func signInStoresTokens() async {
        let (store, _, tokens, _) = await environment()

        await store.signIn(email: "someone@example.com", password: "correct-horse-battery-staple")

        #expect(store.user?.email == "someone@example.com")
        #expect(await tokens.accessToken == "access-new")
        #expect(await tokens.refreshToken == "refresh-new")
    }

    @Test("A rejected sign-in stores nothing and says why")
    func rejectedSignInStoresNothing() async {
        let (store, api, tokens, _) = await environment()
        api.authenticateBehaviour = .rejects(
            .api(
                status: 401,
                envelope: APIErrorEnvelope.Detail(
                    code: "invalid_credentials",
                    message: "Email or password is incorrect.",
                    requestId: "r-1",
                    details: nil
                )
            )
        )

        await store.signIn(email: "someone@example.com", password: "wrong-password")

        #expect(store.state == .restoring)
        #expect(await !tokens.hasSession)
        #expect(store.error?.code == "invalid_credentials")
        // The same message for an unknown email as for a wrong password. The
        // server is careful about that and the app must not undo it.
        #expect(store.error?.userMessage == "Email or password is incorrect.")
    }

    @Test("A signed-out store ignores a profile edit rather than crashing")
    func profileEditNeedsASession() async {
        let (store, _, _, _) = await environment()

        await store.updateTimezone("Asia/Tokyo")

        #expect(store.state == .restoring)
        #expect(store.error == nil)
    }

    @Test("A profile edit the server rejects surfaces rather than being swallowed")
    func profileEditReportsFailure() async {
        let (store, api, _, _) = await environment(stored: storedPair)
        await store.restore()
        api.currentUserBehaviour = .rejects(
            .api(
                status: 422,
                envelope: APIErrorEnvelope.Detail(
                    code: "validation_error",
                    message: "Unknown timezone.",
                    requestId: nil,
                    details: nil
                )
            )
        )

        await store.updateTimezone("Mars/Olympus")

        #expect(store.error?.code == "validation_error")
        // The old profile is kept: a failed edit must not blank the screen.
        #expect(store.user != nil)
    }
}
