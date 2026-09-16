import Foundation

/// The body every non-2xx NOVA response carries.
struct APIErrorEnvelope: Decodable, Sendable {
    struct Detail: Decodable, Sendable {
        /// Stable and machine-readable. Branch on this, never on `message`.
        let code: String
        let message: String
        /// Matches the `X-Request-ID` header and the server logs.
        let requestId: String?
        let details: Details?
    }

    /// The `details` object, which carries whatever a particular error
    /// needs. Every field is optional because most errors use none of them.
    struct Details: Decodable, Sendable {
        let errors: [FieldError]?

        /// Present on `tool_confirmation_required`. Together these are the
        /// whole handshake: what NOVA wants to do, in words, and the token
        /// that authorises exactly that.
        let tool: String?
        let prompt: String?
        let confirmationToken: String?
        let expiresInSeconds: Int?
    }

    /// One field-level validation failure.
    ///
    /// The server deliberately omits the submitted value, so a rejected
    /// password is never echoed back here.
    struct FieldError: Decodable, Sendable {
        let type: String
        let loc: [String]
        let msg: String

        /// The offending field, ignoring the leading "body"/"query" segment.
        var field: String? {
            loc.last
        }
    }

    let error: Detail
}

/// Everything that can go wrong talking to the API.
enum APIError: Error, Sendable {
    /// The server answered with a structured error.
    case api(status: Int, envelope: APIErrorEnvelope.Detail)
    /// The server answered, but not in a shape we understand.
    case undecodable(status: Int, underlying: String)
    /// The request never completed.
    case transport(URLError)
    /// Credentials are gone and could not be renewed.
    case unauthenticated

    /// Text safe to show a person.
    var userMessage: String {
        switch self {
        case let .api(_, envelope):
            envelope.message
        case .undecodable:
            "NOVA sent something unexpected. Try again."
        case let .transport(error):
            switch error.code {
            case .notConnectedToInternet, .networkConnectionLost:
                "No connection."
            case .timedOut:
                "NOVA took too long to respond."
            case .cannotFindHost, .cannotConnectToHost:
                "Can't reach NOVA. Check the server address in Settings."
            default:
                "Something went wrong. Try again."
            }
        case .unauthenticated:
            "Your session ended. Sign in again."
        }
    }

    /// The stable error code, when the server supplied one.
    var code: String? {
        if case let .api(_, envelope) = self { return envelope.code }
        return nil
    }

    var requestId: String? {
        if case let .api(_, envelope) = self { return envelope.requestId }
        return nil
    }

    /// Per-field messages, for showing validation failures inline.
    var fieldErrors: [String: String] {
        guard case let .api(_, envelope) = self,
              let errors = envelope.details?.errors
        else { return [:] }

        return Dictionary(
            errors.compactMap { error in
                error.field.map { ($0, error.msg) }
            },
            uniquingKeysWith: { first, _ in first }
        )
    }

    /// True when re-attempting could plausibly succeed.
    var isRetryable: Bool {
        switch self {
        case .transport:
            true
        case let .api(status, _):
            status >= 500 || status == 429
        case .undecodable, .unauthenticated:
            false
        }
    }
}
