import Foundation

/// A `URLProtocol` that answers from a script instead of the network.
///
/// Registered on a `URLSessionConfiguration` the test builds, so the client
/// under test runs its real code -- building the request, attaching
/// credentials, decoding the response -- with only the wire replaced. A fake
/// `URLSession` subclass would skip all of that, and the parts it skipped are
/// the parts worth testing.
///
/// The recorded requests are what most assertions read: whether an
/// `Authorization` header was attached, how many times `auth/refresh` was
/// asked for, which token the retry carried.
///
/// Scripts are registered per session rather than in one shared slot.
/// swift-testing runs tests in parallel by default, so a single static script
/// would be swapped out from under a test that had already started -- which
/// showed up as every assertion failing against zero recorded requests.
/// `URLSession` constructs the protocol itself and gives it no way to carry a
/// dependency, so the session's identity travels in a header instead.
final class StubTransport: URLProtocol, @unchecked Sendable {
    /// The header each stubbed session stamps on its requests, naming the
    /// script that should answer them.
    static let sessionHeader = "X-Nova-Stub-Session"

    /// What to answer with, and what was asked.
    final class Script: @unchecked Sendable {
        /// Called for each request. Returns the status, body and headers.
        typealias Handler = @Sendable (URLRequest) throws -> (Int, Data, [String: String])

        private let lock = NSLock()
        private var handler: Handler = { _ in (200, Data("{}".utf8), [:]) }
        private var recorded: [URLRequest] = []
        private var recordedBodies: [String] = []

        func respond(with handler: @escaping Handler) {
            lock.withLock { self.handler = handler }
        }

        /// Answer every request with one status and body.
        func respond(status: Int = 200, json: String) {
            respond { _ in (status, Data(json.utf8), ["Content-Type": "application/json"]) }
        }

        /// Fail at the transport layer, the way a phone off the Wi-Fi does.
        func fail(with code: URLError.Code) {
            respond { _ in throw URLError(code) }
        }

        /// Every request this session made, in order.
        var requests: [URLRequest] { lock.withLock { recorded } }

        /// The bodies of those requests, as text.
        var bodies: [String] { lock.withLock { recordedBodies } }

        /// The requests whose path ends in `suffix`, in order.
        func requests(to suffix: String) -> [URLRequest] {
            requests.filter { $0.url?.path().hasSuffix(suffix) == true }
        }

        fileprivate func handle(_ request: URLRequest) throws -> (Int, Data, [String: String]) {
            let handler = lock.withLock { self.handler }
            return try handler(request)
        }

        fileprivate func record(_ request: URLRequest, body: String?) {
            lock.withLock {
                recorded.append(request)
                if let body { recordedBodies.append(body) }
            }
        }
    }

    /// Live scripts, by the identifier their session stamps on each request.
    private final class Registry: @unchecked Sendable {
        private let lock = NSLock()
        private var scripts: [String: Script] = [:]

        func register(_ script: Script, as id: String) {
            lock.withLock { scripts[id] = script }
        }

        func script(for id: String?) -> Script? {
            guard let id else { return nil }
            return lock.withLock { scripts[id] }
        }
    }

    private static let registry = Registry()

    /// A session wired to this protocol, with a script only it can see.
    static func session() -> (URLSession, Script) {
        let script = Script()
        let id = UUID().uuidString
        registry.register(script, as: id)

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubTransport.self]
        configuration.httpAdditionalHeaders = [sessionHeader: id]
        return (URLSession(configuration: configuration), script)
    }

    // MARK: - URLProtocol

    override class func canInit(with request: URLRequest) -> Bool {
        registry.script(for: request.value(forHTTPHeaderField: sessionHeader)) != nil
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let script = Self.registry.script(
                for: request.value(forHTTPHeaderField: Self.sessionHeader)
            )
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }

        // `URLProtocol` moves a request's body into `httpBodyStream`, so
        // reading `httpBody` back gives nil -- drain the stream instead.
        let body: String? =
            if let stream = request.httpBodyStream {
                Self.drain(stream)
            } else if let data = request.httpBody {
                String(decoding: data, as: UTF8.self)
            } else {
                nil
            }
        script.record(request, body: body)

        do {
            let (status, payload, headers) = try script.handle(request)
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: status,
                httpVersion: "HTTP/1.1",
                headerFields: headers
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: payload)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    private static func drain(_ stream: InputStream) -> String {
        stream.open()
        defer { stream.close() }

        var data = Data()
        let size = 4096
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
        defer { buffer.deallocate() }

        while stream.hasBytesAvailable {
            let read = stream.read(buffer, maxLength: size)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return String(decoding: data, as: UTF8.self)
    }
}
