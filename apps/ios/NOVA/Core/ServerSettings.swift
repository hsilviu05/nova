import Foundation
import Observation

/// Where this phone thinks NOVA is.
///
/// The one setting that has to be editable inside the app rather than baked
/// into the build. NOVA runs on a Mac on the same Wi-Fi, and its address is a
/// private IP that changes when the router feels like it — so "reinstall the
/// app with a new base URL" is not an acceptable answer to "my server moved".
///
/// Stored in `UserDefaults`, not the Keychain: an address is configuration,
/// not a credential, and the Keychain has different backup and unlock
/// semantics for no benefit here.
@MainActor
@Observable
final class ServerSettings {
    /// Rejected before it can be saved, so a typo is caught at the moment it
    /// is made rather than as a stream of connection errors afterwards.
    enum ValidationError: Error, Equatable {
        case notAURL
        case missingHost
        case unsupportedScheme
        /// Cleartext to something that is not on the local network.
        ///
        /// A refusal, not a warning. ATS would block the request anyway, and
        /// a setting that saves but never works is worse than one that
        /// explains itself.
        case insecureRemoteHost

        var message: String {
            switch self {
            case .notAURL:
                "That is not an address NOVA can use."
            case .missingHost:
                "Include the host, for example http://192.168.1.20:8000"
            case .unsupportedScheme:
                "Use http:// for a server on your network, or https:// for one on the internet."
            case .insecureRemoteHost:
                "Plain http:// only works for a server on your own network. Use https:// for anything else."
            }
        }
    }

    private static let key = "NOVAServerURL"

    private let defaults: UserDefaults
    private(set) var baseURL: URL

    /// The address NOVA was built with, used until someone changes it.
    let fallback: URL

    init(defaults: UserDefaults = .standard, fallback: URL = APIConfiguration.buildDefault) {
        self.defaults = defaults
        self.fallback = fallback

        if let stored = defaults.string(forKey: Self.key), let url = URL(string: stored) {
            baseURL = url
        } else {
            baseURL = fallback
        }
    }

    var configuration: APIConfiguration {
        APIConfiguration(baseURL: baseURL)
    }

    /// Whether this is the address a build ships with rather than one chosen.
    ///
    /// The Settings screen uses it to explain, once, that the phone will not
    /// reach a Mac at `127.0.0.1` — the single most likely first-run mistake.
    var isLoopback: Bool {
        ["127.0.0.1", "localhost", "::1"].contains(baseURL.host() ?? "")
    }

    /// Normalise and store an address typed by a person.
    ///
    /// Accepts what people actually type: a bare host, a host and port, a
    /// trailing slash, surrounding spaces. Everything else is rejected with a
    /// reason rather than silently coerced.
    @discardableResult
    func update(to raw: String) throws(ValidationError) -> URL {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw .notAURL }

        // A bare "192.168.1.20:8000" parses as a URL with scheme "192.168.1.20",
        // which is why the scheme is added before parsing rather than after.
        let candidate = trimmed.contains("://") ? trimmed : "http://\(trimmed)"

        guard var components = URLComponents(string: candidate) else { throw .notAURL }
        components.path = components.path.hasSuffix("/")
            ? String(components.path.dropLast())
            : components.path

        guard let host = components.host, !host.isEmpty else { throw .missingHost }
        guard let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https"
        else { throw .unsupportedScheme }

        if scheme == "http", !Self.isLocalNetwork(host) {
            throw .insecureRemoteHost
        }

        guard let url = components.url else { throw .notAURL }

        baseURL = url
        defaults.set(url.absoluteString, forKey: Self.key)
        return url
    }

    func reset() {
        baseURL = fallback
        defaults.removeObject(forKey: Self.key)
    }

    /// Whether cleartext to this host is something ATS will actually permit.
    ///
    /// Mirrors what `NSAllowsLocalNetworking` covers: RFC 1918 addresses,
    /// link-local, loopback, `.local`, and unqualified names. Checked here so
    /// the refusal names the problem, instead of the request failing later
    /// with a generic transport error nobody can act on.
    static func isLocalNetwork(_ host: String) -> Bool {
        let lowered = host.lowercased()

        if lowered == "localhost" || lowered == "::1" { return true }
        if lowered.hasSuffix(".local") { return true }
        // No dots at all: a name resolved by Bonjour or a local resolver.
        if !lowered.contains(".") && !lowered.contains(":") { return true }

        let parts = lowered.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4 else { return false }

        switch (parts[0], parts[1]) {
        case (10, _), (127, _), (192, 168):
            return true
        case (172, 16...31):
            return true
        case (169, 254):  // link-local
            return true
        default:
            return false
        }
    }
}
