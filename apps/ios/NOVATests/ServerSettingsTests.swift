import Foundation
import Testing

@testable import NOVA

/// Where the phone thinks NOVA is.
///
/// The single setting that decides whether the app works at all, typed by
/// hand, on a phone, from memory. These are about accepting what people
/// actually type and refusing -- with a reason -- what will not work, rather
/// than saving it and letting the failure show up later as a stream of
/// connection errors nobody can act on.
@MainActor
struct ServerSettingsTests {
    private func settings() -> ServerSettings {
        // A throwaway suite per test, so one test's saved address is not
        // another's starting state.
        let suite = UserDefaults(suiteName: "nova.tests.\(UUID().uuidString)")!
        return ServerSettings(
            defaults: suite, fallback: URL(string: "http://127.0.0.1:8000")!
        )
    }

    @Test("Accepts a Mac on the local network")
    func localAddress() throws {
        let server = settings()

        let url = try server.update(to: "http://192.168.1.20:8000")

        #expect(url.absoluteString == "http://192.168.1.20:8000")
        #expect(server.baseURL == url)
    }

    @Test("Adds the scheme somebody left off")
    func bareHost() throws {
        // "192.168.1.20:8000" parses as a URL whose *scheme* is the IP
        // address, which is why the scheme is added before parsing.
        let url = try settings().update(to: "192.168.1.20:8000")

        #expect(url.scheme == "http")
        #expect(url.host() == "192.168.1.20")
        #expect(url.port == 8000)
    }

    @Test("Tolerates a trailing slash and surrounding spaces")
    func untidyInput() throws {
        let url = try settings().update(to: "  http://192.168.1.20:8000/  ")

        // A trailing slash would double up when paths are appended.
        #expect(url.absoluteString == "http://192.168.1.20:8000")
    }

    @Test("Accepts a .local name from Bonjour")
    func bonjourName() throws {
        let url = try settings().update(to: "http://silviu-mac.local:8000")
        #expect(url.host() == "silviu-mac.local")
    }

    @Test("Accepts HTTPS anywhere")
    func remoteHTTPS() throws {
        let url = try settings().update(to: "https://nova.example.com")
        #expect(url.scheme == "https")
    }

    @Test("Refuses cleartext to somewhere that is not on the local network")
    func insecureRemote() {
        // Not a warning. ATS would block the request anyway, so saving this
        // would produce a setting that looks fine and never works.
        #expect(throws: ServerSettings.ValidationError.insecureRemoteHost) {
            try settings().update(to: "http://nova.example.com")
        }
    }

    @Test("Refuses a scheme that is not HTTP")
    func wrongScheme() {
        #expect(throws: ServerSettings.ValidationError.unsupportedScheme) {
            try settings().update(to: "ws://192.168.1.20:8000")
        }
    }

    @Test("Refuses an empty address")
    func empty() {
        #expect(throws: ServerSettings.ValidationError.notAURL) {
            try settings().update(to: "   ")
        }
    }

    @Test("Every refusal explains itself")
    func messagesAreUseful() {
        // A validation error nobody can act on is worse than none: the
        // person is holding a phone, not a debugger.
        for error in [
            ServerSettings.ValidationError.notAURL,
            .missingHost,
            .unsupportedScheme,
            .insecureRemoteHost,
        ] {
            #expect(!error.message.isEmpty)
        }
        #expect(ServerSettings.ValidationError.insecureRemoteHost.message.contains("https://"))
    }

    @Test("Remembers the address across launches")
    func persistence() throws {
        let suite = UserDefaults(suiteName: "nova.tests.\(UUID().uuidString)")!
        let fallback = URL(string: "http://127.0.0.1:8000")!

        try ServerSettings(defaults: suite, fallback: fallback)
            .update(to: "http://192.168.1.20:8000")
        let relaunched = ServerSettings(defaults: suite, fallback: fallback)

        #expect(relaunched.baseURL.absoluteString == "http://192.168.1.20:8000")
    }

    @Test("Reset goes back to the built-in address")
    func reset() throws {
        let server = settings()
        try server.update(to: "http://192.168.1.20:8000")

        server.reset()

        #expect(server.baseURL == server.fallback)
    }

    @Test("Knows when it is still pointed at the phone itself")
    func loopbackIsFlagged() throws {
        // The most likely first-run mistake: on the phone, 127.0.0.1 is the
        // phone, not the Mac NOVA is running on. The Settings screen says so
        // only when this is true.
        let server = settings()
        #expect(server.isLoopback)

        try server.update(to: "http://192.168.1.20:8000")
        #expect(!server.isLoopback)
    }

    @Test(
        "Recognises the addresses ATS actually permits cleartext to",
        arguments: [
            ("10.0.0.5", true),
            ("172.16.0.1", true),
            ("172.31.255.254", true),
            ("192.168.1.20", true),
            ("169.254.1.1", true),
            ("localhost", true),
            ("silviu-mac.local", true),
            ("nova", true),
            ("172.32.0.1", false),
            ("8.8.8.8", false),
            ("example.com", false),
        ]
    )
    func localNetworkDetection(host: String, expected: Bool) {
        // Mirrors what NSAllowsLocalNetworking covers. Getting 172.16/12
        // wrong is the classic version of this bug: 172.32 is public.
        #expect(ServerSettings.isLocalNetwork(host) == expected)
    }

    @Test("The API configuration follows the configured address")
    func configuration() throws {
        let server = settings()
        try server.update(to: "http://192.168.1.20:8000")

        #expect(
            server.configuration.apiV1.absoluteString
                == "http://192.168.1.20:8000/api/v1"
        )
    }
}
