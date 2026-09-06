import Foundation
import Security

/// Persists the token pair in the Keychain.
///
/// An actor because the Keychain is reached from several tasks — request
/// authentication, renewal, sign-out — and the in-memory cache must not be
/// read while a write is half done.
///
/// Accessibility is `AfterFirstUnlockThisDeviceOnly`: the app needs its
/// credentials in the background (a WebSocket reconnect after the phone has
/// been locked all night), so `WhenUnlocked` is too strict. `ThisDeviceOnly`
/// keeps the tokens out of iCloud Keychain and encrypted backups, which
/// matters because a refresh token is a long-lived credential.
actor TokenStore {
    private let service: String
    private let account = "session"

    private var cached: TokenPair?
    private var loaded = false

    init(service: String = "com.nova.app.tokens") {
        self.service = service
    }

    // MARK: - Reading

    var accessToken: String? {
        get async { await current()?.accessToken }
    }

    var refreshToken: String? {
        get async { await current()?.refreshToken }
    }

    var hasSession: Bool {
        get async { await current() != nil }
    }

    private func current() async -> TokenPair? {
        if !loaded {
            cached = readFromKeychain()
            loaded = true
        }
        return cached
    }

    // MARK: - Writing

    func save(_ pair: TokenPair) {
        cached = pair
        loaded = true

        guard let data = try? JSONCoding.encoder.encode(pair) else { return }

        // Delete then add: SecItemUpdate needs a different query shape, and
        // the two-step version cannot leave a stale item behind.
        SecItemDelete(baseQuery() as CFDictionary)

        var attributes = baseQuery()
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] =
            kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

        SecItemAdd(attributes as CFDictionary, nil)
    }

    func clear() {
        cached = nil
        loaded = true
        SecItemDelete(baseQuery() as CFDictionary)
    }

    // MARK: - Keychain

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    private func readFromKeychain() -> TokenPair? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data
        else { return nil }

        return try? JSONCoding.decoder.decode(TokenPair.self, from: data)
    }
}
