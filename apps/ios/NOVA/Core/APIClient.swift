import Foundation

/// Where the API lives.
struct APIConfiguration: Sendable {
    var baseURL: URL
    var timeout: TimeInterval = 20

    /// The simulator reaching a server on the development machine.
    static let localDevelopment = APIConfiguration(
        baseURL: URL(string: "http://127.0.0.1:8000")!
    )

    var apiV1: URL { baseURL.appending(path: "api/v1") }

    /// The device WebSocket, derived from `baseURL` so one setting configures
    /// both and they cannot drift apart.
    var deviceSocketURL: URL? {
        guard var components = URLComponents(
            url: baseURL.appending(path: "api/v1/devices/ws"),
            resolvingAgainstBaseURL: false
        ) else { return nil }

        components.scheme = switch components.scheme {
        case "https": "wss"
        default: "ws"
        }
        return components.url
    }
}

/// One HTTP request to the API.
struct Request: Sendable {
    enum Method: String, Sendable {
        case get = "GET", post = "POST", patch = "PATCH", delete = "DELETE"
    }

    var method: Method = .get
    var path: String
    var query: [String: String] = [:]
    var body: (any Encodable & Sendable)?
    /// False only for the endpoints that run before there is a session.
    var requiresAuth: Bool = true
}

/// Performs requests, attaching credentials and renewing them when needed.
///
/// An actor because token renewal must not run concurrently: the API rotates
/// refresh tokens and revokes the whole family when a rotated one is replayed
/// (see ADR 006). Two parallel refreshes would replay the same token and sign
/// the user out of every device. Serialising here is what prevents that.
actor APIClient {
    private let configuration: APIConfiguration
    private let session: URLSession
    private let tokens: TokenStore

    /// The in-flight renewal, so concurrent callers await one attempt rather
    /// than starting their own.
    private var renewal: Task<TokenPair, Error>?

    init(
        configuration: APIConfiguration,
        tokens: TokenStore,
        session: URLSession? = nil
    ) {
        self.configuration = configuration
        self.tokens = tokens

        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = configuration.timeout
            config.waitsForConnectivity = false
            self.session = URLSession(configuration: config)
        }
    }

    // MARK: - Sending

    /// Send a request and decode its body.
    func send<Response: Decodable & Sendable>(
        _ request: Request,
        as type: Response.Type = Response.self
    ) async throws -> Response {
        let data = try await perform(request)

        do {
            return try JSONCoding.decoder.decode(Response.self, from: data)
        } catch {
            throw APIError.undecodable(status: 200, underlying: "\(error)")
        }
    }

    /// Send a request that returns no body.
    func send(_ request: Request) async throws {
        _ = try await perform(request)
    }

    // MARK: - Internals

    private func perform(_ request: Request) async throws -> Data {
        let (data, response) = try await execute(request)

        // A 401 on an authenticated call means the access token aged out.
        // Renew once and retry; a second 401 is a real rejection.
        if response.statusCode == 401, request.requiresAuth {
            guard (try? await renewTokens()) != nil else {
                await tokens.clear()
                throw APIError.unauthenticated
            }

            let (retryData, retryResponse) = try await execute(request)
            guard (200..<300).contains(retryResponse.statusCode) else {
                if retryResponse.statusCode == 401 {
                    await tokens.clear()
                    throw APIError.unauthenticated
                }
                throw decodeError(status: retryResponse.statusCode, data: retryData)
            }
            return retryData
        }

        guard (200..<300).contains(response.statusCode) else {
            throw decodeError(status: response.statusCode, data: data)
        }
        return data
    }

    private func execute(_ request: Request) async throws -> (Data, HTTPURLResponse) {
        var urlRequest = try build(request)

        if request.requiresAuth, let access = await tokens.accessToken {
            urlRequest.setValue("Bearer \(access)", forHTTPHeaderField: "Authorization")
        }

        do {
            let (data, response) = try await session.data(for: urlRequest)
            guard let http = response as? HTTPURLResponse else {
                throw APIError.undecodable(status: 0, underlying: "Not an HTTP response")
            }
            return (data, http)
        } catch let error as URLError {
            throw APIError.transport(error)
        }
    }

    private func build(_ request: Request) throws -> URLRequest {
        var components = URLComponents(
            url: configuration.apiV1.appending(path: request.path),
            resolvingAgainstBaseURL: false
        )
        if !request.query.isEmpty {
            components?.queryItems = request.query
                .sorted { $0.key < $1.key }
                .map { URLQueryItem(name: $0.key, value: $0.value) }
        }

        guard let url = components?.url else {
            throw APIError.undecodable(status: 0, underlying: "Bad URL for \(request.path)")
        }

        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = request.method.rawValue
        urlRequest.setValue("application/json", forHTTPHeaderField: "Accept")

        if let body = request.body {
            urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
            urlRequest.httpBody = try JSONCoding.encoder.encode(body)
        }
        return urlRequest
    }

    private func decodeError(status: Int, data: Data) -> APIError {
        guard let envelope = try? JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: data
        ) else {
            let body = String(data: data.prefix(200), encoding: .utf8) ?? "<binary>"
            return .undecodable(status: status, underlying: body)
        }
        return .api(status: status, envelope: envelope.error)
    }

    // MARK: - Token renewal

    /// Exchange the refresh token for a new pair, at most once at a time.
    ///
    /// Callers arriving while a renewal is running await that one instead of
    /// starting another. This is not an optimisation: a second concurrent
    /// refresh would present a token the first has already rotated, which the
    /// server correctly treats as theft and answers by revoking every session
    /// in the family.
    private func renewTokens() async throws -> TokenPair {
        if let renewal {
            return try await renewal.value
        }

        let task = Task<TokenPair, Error> {
            guard let refresh = await tokens.refreshToken else {
                throw APIError.unauthenticated
            }

            let request = Request(
                method: .post,
                path: "auth/refresh",
                body: RefreshRequest(refreshToken: refresh),
                requiresAuth: false
            )

            let (data, response) = try await execute(request)
            guard (200..<300).contains(response.statusCode) else {
                throw decodeError(status: response.statusCode, data: data)
            }

            let pair = try JSONCoding.decoder.decode(TokenPair.self, from: data)
            await tokens.save(pair)
            return pair
        }

        renewal = task
        defer { renewal = nil }
        return try await task.value
    }

    /// Force a renewal, used by the session restore path at launch.
    func refreshSession() async throws {
        _ = try await renewTokens()
    }
}
