import Foundation
import Observation

/// Who is signed in, and whether we know yet.
@MainActor
@Observable
final class SessionStore {
    enum State: Equatable {
        /// Checking the Keychain at launch. The UI shows nothing decisive
        /// during this, so a returning user never sees a sign-in screen flash
        /// before their session is restored.
        case restoring
        case signedOut
        case signedIn(User)
    }

    private(set) var state: State = .restoring
    private(set) var isWorking = false
    var error: APIError?

    private let api: any NovaAPI
    private let tokens: TokenStore
    private let client: APIClient

    init(api: any NovaAPI, tokens: TokenStore, client: APIClient) {
        self.api = api
        self.tokens = tokens
        self.client = client
    }

    var user: User? {
        if case let .signedIn(user) = state { return user }
        return nil
    }

    // MARK: - Launch

    /// Restore a session from the Keychain, if there is one.
    ///
    /// A stored token is not assumed valid: the server is asked. That is what
    /// makes an account deactivated or a device released while the app was
    /// closed take effect at launch rather than whenever the token expires.
    func restore() async {
        guard await tokens.hasSession else {
            state = .signedOut
            return
        }

        do {
            state = .signedIn(try await api.currentUser())
        } catch {
            // The access token may simply have aged out while the app was
            // closed; renewal is the normal path here, not an error.
            do {
                try await client.refreshSession()
                state = .signedIn(try await api.currentUser())
            } catch {
                await tokens.clear()
                state = .signedOut
            }
        }
    }

    // MARK: - Sign in and out

    func signIn(email: String, password: String) async {
        await authenticate {
            try await self.api.signIn(
                email: email.trimmingCharacters(in: .whitespacesAndNewlines),
                password: password
            )
        }
    }

    func register(email: String, password: String, displayName: String) async {
        await authenticate {
            try await self.api.register(
                email: email.trimmingCharacters(in: .whitespacesAndNewlines),
                password: password,
                displayName: displayName.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
    }

    func signOut() async {
        // Tell the server first so the refresh family is revoked, then clear
        // locally regardless. A failed network call must not strand someone
        // in a session they asked to leave.
        if let refresh = await tokens.refreshToken {
            try? await api.signOut(refreshToken: refresh)
        }
        await tokens.clear()
        error = nil
        state = .signedOut
    }

    func updateDisplayName(_ name: String) async {
        await updateProfile(displayName: name, timezone: nil)
    }

    /// Change the timezone every analytics figure is bucketed in.
    func updateTimezone(_ identifier: String) async {
        await updateProfile(displayName: nil, timezone: identifier)
    }

    private func updateProfile(displayName: String?, timezone: String?) async {
        guard case .signedIn = state else { return }
        do {
            state = .signedIn(
                try await api.updateProfile(displayName: displayName, timezone: timezone)
            )
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    private func authenticate(
        _ operation: @escaping () async throws -> AuthenticatedUser
    ) async {
        isWorking = true
        error = nil
        defer { isWorking = false }

        do {
            let result = try await operation()
            await tokens.save(result.tokens)
            state = .signedIn(result.user)
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }
}
