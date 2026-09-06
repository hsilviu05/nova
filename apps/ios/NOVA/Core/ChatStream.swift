import Foundation

/// One event from the reply stream.
enum ChatStreamEvent: Sendable, Equatable {
    /// The stored user turn, echoed first so the optimistic copy can be
    /// replaced by the real one.
    case message(ChatMessage)
    /// A fragment of the reply.
    case delta(String)
    /// The reply is complete.
    case done
    /// The provider failed before producing anything.
    case failed(code: String, message: String)
}

/// Reads NOVA's replies as Server-Sent Events.
///
/// Hand-rolled rather than pulling in a dependency: SSE is a line-oriented
/// text format, and `URLSession.bytes(for:)` already delivers the lines. The
/// whole parser is the function below.
struct ChatStreamClient: Sendable {
    private let configuration: APIConfiguration
    private let tokens: TokenStore
    private let session: URLSession

    init(configuration: APIConfiguration, tokens: TokenStore) {
        self.configuration = configuration
        self.tokens = tokens

        let config = URLSessionConfiguration.ephemeral
        // No overall timeout: a reply legitimately takes as long as the model
        // takes to think. The per-resource idle timeout below is what
        // catches a genuinely dead connection.
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 600
        self.session = URLSession(configuration: config)
    }

    /// Send a message and stream the reply.
    ///
    /// Throws before yielding anything if the request itself fails -- an
    /// unknown conversation, an expired session -- so those surface as
    /// ordinary errors rather than as an event inside a stream the UI has
    /// already begun rendering.
    func send(
        _ content: String, to conversationID: UUID
    ) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await run(content, conversationID, continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            // Cancelling the stream cancels the request, which lets the
            // server record the partial reply and stop paying for the rest.
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func run(
        _ content: String,
        _ conversationID: UUID,
        _ continuation: AsyncThrowingStream<ChatStreamEvent, Error>.Continuation
    ) async throws {
        var request = URLRequest(
            url: configuration.apiV1.appending(
                path: "conversations/\(conversationID.uuidString.lowercased())/stream"
            )
        )
        request.httpMethod = "POST"
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONCoding.encoder.encode(
            SendMessageRequest(content: content)
        )

        if let access = await tokens.accessToken {
            request.setValue("Bearer \(access)", forHTTPHeaderField: "Authorization")
        }

        let (bytes, response) = try await session.bytes(for: request)

        guard let http = response as? HTTPURLResponse else {
            throw APIError.undecodable(status: 0, underlying: "Not an HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw try await Self.error(from: bytes, status: http.statusCode)
        }

        var eventName = ""
        var data = ""

        for try await line in bytes.lines {
            if line.isEmpty {
                // A blank line terminates an event.
                if let event = Self.parse(name: eventName, data: data) {
                    continuation.yield(event)
                }
                eventName = ""
                data = ""
            } else if let value = line.dropPrefix("event: ") {
                eventName = value
            } else if let value = line.dropPrefix("data: ") {
                data = value
            }
            // Anything else (comments, retry hints) is ignored by design.
        }

        // A stream that ends without its terminator was cut short. The
        // server has already stored whatever it produced, so this is not an
        // error worth surfacing -- the UI simply stops receiving deltas.
        if let event = Self.parse(name: eventName, data: data) {
            continuation.yield(event)
        }
    }

    private static func parse(name: String, data: String) -> ChatStreamEvent? {
        guard !name.isEmpty, let payload = data.data(using: .utf8) else {
            return nil
        }

        switch name {
        case "message":
            return (try? JSONCoding.decoder.decode(ChatMessage.self, from: payload))
                .map(ChatStreamEvent.message)

        case "delta":
            struct Delta: Decodable { let text: String }
            return (try? JSONCoding.decoder.decode(Delta.self, from: payload))
                .map { .delta($0.text) }

        case "done":
            return .done

        case "error":
            struct Failure: Decodable {
                let code: String
                let message: String
            }
            return (try? JSONCoding.decoder.decode(Failure.self, from: payload))
                .map { .failed(code: $0.code, message: $0.message) }

        default:
            // An event type this client does not know about yet. Ignored
            // rather than treated as a failure, so the server can add one
            // without breaking older builds.
            return nil
        }
    }

    /// Read an error body from a failed response.
    private static func error(
        from bytes: URLSession.AsyncBytes, status: Int
    ) async throws -> APIError {
        var body = Data()
        for try await byte in bytes {
            body.append(byte)
            // Error envelopes are small; anything larger is not one.
            if body.count > 8192 { break }
        }

        guard let envelope = try? JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: body
        ) else {
            return .undecodable(
                status: status,
                underlying: String(data: body.prefix(200), encoding: .utf8) ?? ""
            )
        }
        return .api(status: status, envelope: envelope.error)
    }
}

private extension String {
    /// The remainder after `prefix`, or nil when it does not match.
    func dropPrefix(_ prefix: String) -> String? {
        hasPrefix(prefix) ? String(dropFirst(prefix.count)) : nil
    }
}
