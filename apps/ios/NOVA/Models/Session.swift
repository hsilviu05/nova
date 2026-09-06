import Foundation

/// The authenticated account.
struct User: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let email: String
    let displayName: String
    /// IANA name. Every hour-of-day figure on the Insights screen is bucketed
    /// server-side in this, so it is part of the profile rather than a
    /// display preference.
    let timezone: String
    let isActive: Bool
    let createdAt: Date
    let lastLoginAt: Date?
}

/// A pair of credentials issued by the API.
///
/// `refreshToken` is shown by the server exactly once; it is written straight
/// to the Keychain and never held anywhere else.
struct TokenPair: Codable, Equatable, Sendable {
    let accessToken: String
    let refreshToken: String
    let tokenType: String
    /// Access-token lifetime in seconds.
    let expiresIn: Int

    /// When this access token stops being accepted.
    ///
    /// Computed on receipt rather than stored by the server, so it is only as
    /// accurate as the device clock. It is used to refresh *early*, never to
    /// decide that a token is valid — that judgement belongs to the server.
    func expiry(from now: Date = .now) -> Date {
        now.addingTimeInterval(TimeInterval(expiresIn))
    }
}

/// The response to registration and sign-in.
struct AuthenticatedUser: Codable, Equatable, Sendable {
    let user: User
    let tokens: TokenPair
}

// MARK: - Requests

struct RegisterRequest: Encodable, Sendable {
    let email: String
    let password: String
    let displayName: String
}

struct LoginRequest: Encodable, Sendable {
    let email: String
    let password: String
}

struct RefreshRequest: Encodable, Sendable {
    let refreshToken: String
}

struct LogoutRequest: Encodable, Sendable {
    let refreshToken: String
}

/// A profile change. Both fields optional: omitted means unchanged, so the
/// app sends only what was edited rather than echoing the whole profile back.
struct UpdateProfileRequest: Encodable, Sendable {
    var displayName: String?
    var timezone: String?
}
