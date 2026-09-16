import Foundation
import Security
import Testing

@testable import NOVA

/// The Keychain, against the real Keychain.
///
/// Not a fake. What is being tested is the query shapes and the accessibility
/// class, and a fake store would agree with whatever the test expected --
/// including if the app asked for the wrong protection class, which is the
/// one thing here that cannot be seen by reading the app's own behaviour.
///
/// A refresh token is a long-lived credential. It grants a new access token
/// on demand, and NOVA's server rotates rather than expires them, so one
/// lifted off a device stays useful until somebody notices. That is why these
/// tests care about where it is written as much as whether it round-trips.
struct TokenStoreTests {
    /// A unique service per test, so one test's item is never another's
    /// starting state and a failure leaves nothing behind.
    private func store() -> TokenStore {
        TokenStore(service: "com.nova.app.tests.\(UUID().uuidString)")
    }

    private func pair(
        access: String = "access-token", refresh: String = "refresh-token"
    ) -> TokenPair {
        TokenPair(
            accessToken: access,
            refreshToken: refresh,
            tokenType: "bearer",
            expiresIn: 900
        )
    }

    // MARK: - Round trip

    @Test("A saved pair survives a new store reading the same service")
    func survivesRelaunch() async {
        // A fresh TokenStore is what the app has at launch: an empty cache
        // and a Keychain that may or may not hold something. Reading through
        // a second instance is the only way to test the Keychain path rather
        // than the in-memory one.
        let service = "com.nova.app.tests.\(UUID().uuidString)"
        await TokenStore(service: service).save(pair())
        defer { Task { await TokenStore(service: service).clear() } }

        let relaunched = TokenStore(service: service)

        #expect(await relaunched.accessToken == "access-token")
        #expect(await relaunched.refreshToken == "refresh-token")
        #expect(await relaunched.hasSession)
    }

    @Test("An empty store reports no session rather than an empty token")
    func noSession() async {
        let tokens = store()

        #expect(await tokens.accessToken == nil)
        #expect(await tokens.refreshToken == nil)
        #expect(await !tokens.hasSession)
    }

    @Test("Saving twice replaces rather than accumulating")
    func rotationReplaces() async {
        // Every refresh writes a new pair. Two items under one account would
        // make which one `SecItemCopyMatching` returns undefined, and half
        // the time the app would present a token the server has revoked --
        // which it reads as theft and answers by killing the family.
        let service = "com.nova.app.tests.\(UUID().uuidString)"
        let tokens = TokenStore(service: service)
        defer { Task { await tokens.clear() } }

        await tokens.save(pair(access: "first", refresh: "first-refresh"))
        await tokens.save(pair(access: "second", refresh: "second-refresh"))

        #expect(await TokenStore(service: service).accessToken == "second")

        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecMatchLimit as String: kSecMatchLimitAll,
        ]
        query[kSecReturnAttributes as String] = true

        var items: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &items)

        #expect(status == errSecSuccess)
        #expect((items as? [[String: Any]])?.count == 1)
    }

    @Test("Signing out leaves nothing behind for the next person")
    func clearRemovesTheItem() async {
        let service = "com.nova.app.tests.\(UUID().uuidString)"
        let tokens = TokenStore(service: service)
        await tokens.save(pair())

        await tokens.clear()

        #expect(await !tokens.hasSession)
        // And it is gone from the Keychain, not only from the cache -- the
        // difference matters at the next launch.
        #expect(await !TokenStore(service: service).hasSession)
    }

    @Test("Clearing a store that holds nothing is not an error")
    func clearIsIdempotent() async {
        let tokens = store()

        await tokens.clear()
        await tokens.clear()

        #expect(await !tokens.hasSession)
    }

    // MARK: - Where it is written

    @Test("Tokens are kept on this device and only after first unlock")
    func accessibilityClass() async throws {
        // The assertion this file exists for.
        //
        // `ThisDeviceOnly` keeps the refresh token out of iCloud Keychain and
        // out of encrypted backups: a credential that grants new sessions
        // should not be restorable onto a different phone from a backup.
        //
        // `AfterFirstUnlock` rather than `WhenUnlocked` because the app reads
        // its credentials while the screen is off -- a dashboard refresh, a
        // reconnect after the phone has been on a desk all night. Requiring
        // an unlocked device would make those fail with nothing to show for
        // it. After the first unlock since boot is the weakest class that
        // still keeps the data encrypted while the phone is off.
        let service = "com.nova.app.tests.\(UUID().uuidString)"
        let tokens = TokenStore(service: service)
        await tokens.save(pair())
        defer { Task { await tokens.clear() } }

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: "session",
            kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        #expect(status == errSecSuccess)

        let attributes = try #require(item as? [String: Any])
        let accessible = attributes[kSecAttrAccessible as String] as? String

        #expect(accessible == (kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String))
    }

    @Test("Two services do not see each other's tokens")
    func servicesAreIsolated() async {
        // The service string is the whole of the separation. If it were
        // ignored, a test run would overwrite the real app's session on a
        // developer's simulator -- and, worse, the isolation this file
        // depends on would be an illusion.
        let mine = TokenStore(service: "com.nova.app.tests.\(UUID().uuidString)")
        let theirs = TokenStore(service: "com.nova.app.tests.\(UUID().uuidString)")
        defer {
            Task {
                await mine.clear()
                await theirs.clear()
            }
        }

        await mine.save(pair(access: "mine"))

        #expect(await theirs.accessToken == nil)
        #expect(await mine.accessToken == "mine")
    }

    // MARK: - Concurrency

    @Test("Concurrent reads and writes never observe a half-written pair")
    func concurrentAccessIsCoherent() async {
        // The reason TokenStore is an actor. Request authentication, renewal
        // and sign-out all reach it from different tasks; a pair read halfway
        // through a write would pair a new access token with an old refresh
        // token, and presenting that rotated refresh token is exactly what
        // the server treats as theft.
        let service = "com.nova.app.tests.\(UUID().uuidString)"
        let tokens = TokenStore(service: service)
        defer { Task { await tokens.clear() } }

        await withTaskGroup(of: Void.self) { group in
            for index in 0..<20 {
                group.addTask {
                    await tokens.save(
                        TokenPair(
                            accessToken: "access-\(index)",
                            refreshToken: "refresh-\(index)",
                            tokenType: "bearer",
                            expiresIn: 900
                        )
                    )
                }
                group.addTask {
                    _ = await tokens.accessToken
                }
            }
        }

        let access = await tokens.accessToken
        let refresh = await tokens.refreshToken
        let accessIndex = access?.dropFirst("access-".count)
        let refreshIndex = refresh?.dropFirst("refresh-".count)

        // Which write won is a race and does not matter. That both halves
        // came from the *same* write is the whole point.
        #expect(accessIndex != nil)
        #expect(accessIndex == refreshIndex)
    }
}
