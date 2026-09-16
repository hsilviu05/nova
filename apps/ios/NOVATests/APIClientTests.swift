import Foundation
import Testing

@testable import NOVA

/// The client that carries the session, against a stubbed wire.
///
/// Most of this file is about one property: renewal runs at most once at a
/// time. NOVA's server rotates refresh tokens and treats a replayed one as
/// theft, revoking the whole family — so two parallel renewals do not merely
/// waste a round trip, they sign the person out of every device they own. The
/// `APIClient` is an actor and holds the in-flight renewal for exactly that
/// reason, and `serialisedRenewal` below is the test that would catch its
/// removal.
///
/// The rest is the 401 ladder and what happens to a response body that is not
/// what the app expected.
struct APIClientTests {
    private func client(
        session: URLSession, tokens: TokenStore
    ) -> APIClient {
        APIClient(
            configuration: APIConfiguration(
                baseURL: URL(string: "http://192.168.1.20:8000")!
            ),
            tokens: tokens,
            session: session
        )
    }

    private func tokens(
        access: String = "access-1", refresh: String = "refresh-1"
    ) async -> TokenStore {
        let store = TokenStore(service: "com.nova.app.tests.\(UUID().uuidString)")
        await store.save(
            TokenPair(
                accessToken: access,
                refreshToken: refresh,
                tokenType: "bearer",
                expiresIn: 900
            )
        )
        return store
    }

    private func emptyTokens() -> TokenStore {
        TokenStore(service: "com.nova.app.tests.\(UUID().uuidString)")
    }

    private struct Probe: Decodable, Equatable {
        let ok: Bool
    }

    // MARK: - Credentials on the wire

    @Test("An authenticated call carries the access token")
    func attachesBearer() async throws {
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)
        let store = await tokens()

        _ = try await client(session: session, tokens: store)
            .send(Request(path: "users/me"), as: Probe.self)

        let sent = try #require(script.requests.first)
        #expect(sent.value(forHTTPHeaderField: "Authorization") == "Bearer access-1")
    }

    @Test("A call made before there is a session carries no credential")
    func skipsBearerWhenNotRequired() async throws {
        // Registration and sign-in. Sending an empty or stale bearer here
        // would make the server answer 401 to the very request meant to
        // produce a session.
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)

        _ = try await client(session: session, tokens: await tokens())
            .send(Request(path: "auth/login", requiresAuth: false), as: Probe.self)

        let sent = try #require(script.requests.first)
        #expect(sent.value(forHTTPHeaderField: "Authorization") == nil)
    }

    @Test("A signed-out client sends no Authorization header at all")
    func noHeaderWithoutTokens() async throws {
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)

        _ = try await client(session: session, tokens: emptyTokens())
            .send(Request(path: "users/me"), as: Probe.self)

        let sent = try #require(script.requests.first)
        #expect(sent.value(forHTTPHeaderField: "Authorization") == nil)
    }

    @Test("The renewal request itself is unauthenticated")
    func renewalCarriesNoBearer() async throws {
        // It presents the refresh token in its body. Attaching the expired
        // access token as well would be pointless and would make the failure
        // mode on the server ambiguous.
        let (session, script) = StubTransport.session()
        script.respond { request in
            if request.url?.path().hasSuffix("auth/refresh") == true {
                return (
                    200,
                    Data(#"{"access_token":"access-2","refresh_token":"refresh-2","token_type":"bearer","expires_in":900}"#.utf8),
                    [:]
                )
            }
            return (401, Data(), [:])
        }

        _ = try? await client(session: session, tokens: await tokens())
            .send(Request(path: "users/me"), as: Probe.self)

        let renewal = try #require(script.requests(to: "auth/refresh").first)
        #expect(renewal.value(forHTTPHeaderField: "Authorization") == nil)
        #expect(renewal.httpMethod == "POST")
    }

    // MARK: - The 401 ladder

    @Test("A 401 renews once and retries with the new token")
    func renewsAndRetries() async throws {
        let (session, script) = StubTransport.session()
        let store = await tokens()

        // The nonisolated(unsafe) box is the simplest way to let the handler
        // — which URLSession calls on its own queue — advance a counter.
        nonisolated(unsafe) var attempts = 0
        script.respond { request in
            let path = request.url?.path() ?? ""
            if path.hasSuffix("auth/refresh") {
                return (
                    200,
                    Data(#"{"access_token":"access-2","refresh_token":"refresh-2","token_type":"bearer","expires_in":900}"#.utf8),
                    [:]
                )
            }
            attempts += 1
            return attempts == 1
                ? (401, Data(), [:])
                : (200, Data(#"{"ok": true}"#.utf8), [:])
        }

        let probe = try await client(session: session, tokens: store)
            .send(Request(path: "users/me"), as: Probe.self)

        #expect(probe == Probe(ok: true))
        #expect(script.requests(to: "auth/refresh").count == 1)

        let retry = try #require(script.requests(to: "users/me").last)
        // The retry carries the *new* token. Retrying with the expired one
        // would produce a second 401 and sign the person out for no reason.
        #expect(retry.value(forHTTPHeaderField: "Authorization") == "Bearer access-2")
        #expect(await store.accessToken == "access-2")
    }

    @Test("A second 401 after renewal ends the session rather than looping")
    func secondRejectionClears() async throws {
        // The account was deactivated, or every session was revoked while the
        // app was closed. Retrying forever would be a loop; keeping the
        // tokens would mean every later request repeats it.
        let (session, script) = StubTransport.session()
        let store = await tokens()

        script.respond { request in
            if request.url?.path().hasSuffix("auth/refresh") == true {
                return (
                    200,
                    Data(#"{"access_token":"access-2","refresh_token":"refresh-2","token_type":"bearer","expires_in":900}"#.utf8),
                    [:]
                )
            }
            return (401, Data(), [:])
        }

        await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: store)
                .send(Request(path: "users/me"), as: Probe.self)
        }

        #expect(script.requests(to: "auth/refresh").count == 1)
        #expect(script.requests(to: "users/me").count == 2)
        #expect(await !store.hasSession)
    }

    @Test("A renewal the server refuses ends the session")
    func failedRenewalClears() async throws {
        let (session, script) = StubTransport.session()
        let store = await tokens()

        script.respond { request in
            if request.url?.path().hasSuffix("auth/refresh") == true {
                return (
                    401,
                    Data(#"{"error":{"code":"token_reused","message":"Session revoked.","request_id":null,"details":null}}"#.utf8),
                    [:]
                )
            }
            return (401, Data(), [:])
        }

        await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: store)
                .send(Request(path: "users/me"), as: Probe.self)
        }

        #expect(await !store.hasSession)
        // Not retried: there is nothing to retry with.
        #expect(script.requests(to: "users/me").count == 1)
    }

    @Test("A client with no refresh token does not attempt a renewal")
    func nothingToRenewWith() async throws {
        let (session, script) = StubTransport.session()
        script.respond(status: 401, json: "{}")

        await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "users/me"), as: Probe.self)
        }

        #expect(script.requests(to: "auth/refresh").isEmpty)
    }

    @Test("A 401 on an unauthenticated call is reported, not renewed")
    func wrongPasswordIsNotARenewal() async throws {
        // A failed sign-in is a 401 too. Treating it as an expired session
        // would send a refresh token that has nothing to do with it.
        let (session, script) = StubTransport.session()
        script.respond(
            status: 401,
            json: #"{"error":{"code":"invalid_credentials","message":"Email or password is incorrect.","request_id":"r-1","details":null}}"#
        )

        let error = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "auth/login", requiresAuth: false), as: Probe.self)
        }

        #expect(error?.code == "invalid_credentials")
        #expect(script.requests(to: "auth/refresh").isEmpty)
    }

    // MARK: - The property this file exists for

    @Test("Concurrent 401s produce exactly one renewal")
    func serialisedRenewal() async throws {
        // Open the app after a night on the desk: the dashboard, the
        // conversation list and the tool list all fire at once, all with the
        // same expired access token, all getting a 401 within milliseconds of
        // each other.
        //
        // Without serialisation each would present the same refresh token.
        // The first rotates it; the rest replay a token the server has
        // already retired, which it correctly reads as a stolen credential
        // and answers by revoking every session in the family. The person is
        // signed out of their phone and their laptop by opening the app.
        let (session, script) = StubTransport.session()
        let store = await tokens()

        nonisolated(unsafe) var renewals = 0
        script.respond { request in
            let path = request.url?.path() ?? ""
            if path.hasSuffix("auth/refresh") {
                renewals += 1
                // A real renewal is a network round trip. Without some delay
                // the first would finish before the others even arrive, and
                // the test would pass whether or not anything serialised it.
                Thread.sleep(forTimeInterval: 0.05)
                return (
                    200,
                    Data(#"{"access_token":"access-2","refresh_token":"refresh-2","token_type":"bearer","expires_in":900}"#.utf8),
                    [:]
                )
            }
            let authorization = request.value(forHTTPHeaderField: "Authorization")
            return authorization == "Bearer access-2"
                ? (200, Data(#"{"ok": true}"#.utf8), [:])
                : (401, Data(), [:])
        }

        let api = client(session: session, tokens: store)

        try await withThrowingTaskGroup(of: Probe.self) { group in
            for path in ["system/status", "conversations", "tools", "memories", "users/me"] {
                group.addTask {
                    try await api.send(Request(path: path), as: Probe.self)
                }
            }
            for try await probe in group {
                #expect(probe == Probe(ok: true))
            }
        }

        #expect(renewals == 1)
        #expect(script.requests(to: "auth/refresh").count == 1)
        #expect(await store.accessToken == "access-2")
    }

    @Test("A renewal that fails is not cached for the next caller")
    func aFailedRenewalIsNotSticky() async throws {
        // The in-flight task is cleared whether it succeeded or threw. If a
        // failed one were held, a single blip while the Wi-Fi was switching
        // would make every later request fail against a stored error until
        // the app was restarted.
        let (session, script) = StubTransport.session()
        let store = await tokens()

        nonisolated(unsafe) var renewals = 0
        script.respond { request in
            if request.url?.path().hasSuffix("auth/refresh") == true {
                renewals += 1
                if renewals == 1 { throw URLError(.networkConnectionLost) }
                return (
                    200,
                    Data(#"{"access_token":"access-2","refresh_token":"refresh-2","token_type":"bearer","expires_in":900}"#.utf8),
                    [:]
                )
            }
            return request.value(forHTTPHeaderField: "Authorization") == "Bearer access-2"
                ? (200, Data(#"{"ok": true}"#.utf8), [:])
                : (401, Data(), [:])
        }

        let api = client(session: session, tokens: store)

        await #expect(throws: APIError.self) {
            _ = try await api.send(Request(path: "users/me"), as: Probe.self)
        }
        // The first attempt cleared the session, so restore one and try
        // again: the second renewal must actually be attempted.
        await store.save(
            TokenPair(
                accessToken: "access-1",
                refreshToken: "refresh-1",
                tokenType: "bearer",
                expiresIn: 900
            )
        )

        let probe = try await api.send(Request(path: "users/me"), as: Probe.self)

        #expect(probe == Probe(ok: true))
        #expect(renewals == 2)
    }

    // MARK: - Reading what came back

    @Test("A structured error keeps its code and request id")
    func decodesTheEnvelope() async throws {
        // The code is what the app branches on; the request id is what makes
        // a screenshot of the failure traceable to a server log line.
        let (session, script) = StubTransport.session()
        script.respond(
            status: 422,
            json: #"""
            {"error":{"code":"validation_error","message":"That is not valid.","request_id":"req-42",
            "details":{"errors":[{"type":"string_too_short","loc":["body","password"],"msg":"too short"}]}}}
            """#
        )

        let error = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "auth/register", requiresAuth: false), as: Probe.self)
        }

        #expect(error?.code == "validation_error")
        #expect(error?.requestId == "req-42")
        #expect(error?.fieldErrors["password"] == "too short")
        #expect(error?.isRetryable == false)
    }

    @Test("A body that is not an envelope is reported without being trusted")
    func undecodableBody() async throws {
        // A proxy's error page, or a captive portal. The first 200 characters
        // are kept so somebody can tell what answered, and no more: this
        // string reaches a UI and sometimes a log.
        let (session, script) = StubTransport.session()
        let page = String(repeating: "x", count: 5000)
        script.respond(status: 502, json: "<html>\(page)</html>")

        let error = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "users/me", requiresAuth: false), as: Probe.self)
        }

        guard case let .undecodable(status, underlying) = try #require(error) else {
            Issue.record("expected .undecodable, got \(String(describing: error))")
            return
        }
        #expect(status == 502)
        #expect(underlying.count <= 200)
        #expect(error?.code == nil)
        // Retrying an HTML error page is not going to help.
        #expect(error?.isRetryable == false)
    }

    @Test("A 200 whose body is the wrong shape is a failure, not a default")
    func undecodableSuccess() async throws {
        // Decoding it into a zero value would put a dashboard full of zeroes
        // on screen and call it a reading.
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"unexpected": 1}"#)

        let error = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "users/me", requiresAuth: false), as: Probe.self)
        }

        guard case .undecodable = try #require(error) else {
            Issue.record("expected .undecodable, got \(String(describing: error))")
            return
        }
    }

    @Test("A server error is worth retrying and a rejection is not")
    func retryability() async throws {
        let (session, script) = StubTransport.session()

        script.respond(
            status: 503,
            json: #"{"error":{"code":"service_unavailable","message":"Try later.","request_id":null,"details":null}}"#
        )
        let unavailable = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "system/status", requiresAuth: false), as: Probe.self)
        }
        #expect(unavailable?.isRetryable == true)

        script.respond(
            status: 403,
            json: #"{"error":{"code":"tool_not_permitted","message":"No.","request_id":null,"details":null}}"#
        )
        let refused = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "tools/invoke", requiresAuth: false), as: Probe.self)
        }
        #expect(refused?.isRetryable == false)
    }

    @Test("A phone off the Wi-Fi gets a sentence about the network")
    func transportFailure() async throws {
        let (session, script) = StubTransport.session()
        script.fail(with: .cannotConnectToHost)

        let error = await #expect(throws: APIError.self) {
            _ = try await self.client(session: session, tokens: self.emptyTokens())
                .send(Request(path: "system/status", requiresAuth: false), as: Probe.self)
        }

        guard case .transport = try #require(error) else {
            Issue.record("expected .transport, got \(String(describing: error))")
            return
        }
        #expect(error?.userMessage.contains("Settings") == true)
        #expect(error?.isRetryable == true)
    }

    // MARK: - Building the request

    @Test("The path is appended under the versioned prefix")
    func buildsTheURL() async throws {
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)

        _ = try await client(session: session, tokens: emptyTokens())
            .send(Request(path: "conversations", requiresAuth: false), as: Probe.self)

        let sent = try #require(script.requests.first)
        #expect(sent.url?.absoluteString == "http://192.168.1.20:8000/api/v1/conversations")
    }

    @Test("Query items come out in a stable order")
    func sortedQuery() async throws {
        // Dictionary order is not stable between runs. Sorting keeps a URL
        // comparable in a test and a log line, and keeps any cache key that
        // is ever derived from it from varying for no reason.
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)

        _ = try await client(session: session, tokens: emptyTokens())
            .send(
                Request(
                    path: "system/activity",
                    query: ["offset": "20", "limit": "5", "tool_name": "git_status"],
                    requiresAuth: false
                ),
                as: Probe.self
            )

        let sent = try #require(script.requests.first)
        #expect(sent.url?.query() == "limit=5&offset=20&tool_name=git_status")
    }

    @Test("A body is sent as JSON with the matching content type")
    func encodesTheBody() async throws {
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)

        _ = try await client(session: session, tokens: emptyTokens())
            .send(
                Request(
                    method: .post,
                    path: "auth/login",
                    body: LoginRequest(email: "a@example.com", password: "hunter2hunter2"),
                    requiresAuth: false
                ),
                as: Probe.self
            )

        let sent = try #require(script.requests.first)
        #expect(sent.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(script.bodies.first?.contains("a@example.com") == true)
    }

    @Test("A call that legitimately takes longer can say so")
    func perRequestTimeout() async throws {
        // A tool that shells out, or a dashboard probing a model that has to
        // load several gigabytes first. The client default is 20 seconds,
        // which is right for everything else.
        let (session, script) = StubTransport.session()
        script.respond(json: #"{"ok": true}"#)

        _ = try await client(session: session, tokens: emptyTokens())
            .send(
                Request(path: "tools/invoke", requiresAuth: false, timeout: 120),
                as: Probe.self
            )

        let sent = try #require(script.requests.first)
        #expect(sent.timeoutInterval == 120)
    }

    @Test("A call with no response body does not try to decode one")
    func emptyResponse() async throws {
        let (session, script) = StubTransport.session()
        script.respond { _ in (204, Data(), [:]) }

        try await client(session: session, tokens: emptyTokens())
            .send(Request(method: .delete, path: "memories/1", requiresAuth: false))

        #expect(script.requests.count == 1)
    }
}
