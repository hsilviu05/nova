import Foundation

/// One event from the reply stream.
enum ChatStreamEvent: Sendable, Equatable {
    /// The stored user turn, echoed first so the optimistic copy can be
    /// replaced by the real one.
    case message(ChatMessage)
    /// A fragment of the reply.
    case delta(String)
    /// NOVA has begun running a tool.
    case toolStarted(callID: String, name: String)
    /// A tool has returned.
    case toolFinished(callID: String, name: String, summary: String, isError: Bool)
    /// NOVA wants to do something that needs the person's approval. Carries
    /// the token that authorises exactly this call and nothing else.
    case confirmationNeeded(PendingConfirmation)
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

    /// - Parameter session: injectable for the same reason `APIClient`'s is:
    ///   the parsing below is the part worth testing, and testing it against
    ///   a real server would make it a test of the server.
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
            // No short overall timeout: a reply legitimately takes as long as
            // the model takes to think, and a local model on a laptop that
            // has just woken up is slow before it is fast. The per-resource
            // timeout is what catches a genuinely dead connection.
            config.timeoutIntervalForRequest = 180
            config.timeoutIntervalForResource = 900
            config.waitsForConnectivity = false
            self.session = URLSession(configuration: config)
        }
    }

    /// Send a message and stream the reply.
    ///
    /// Throws before yielding anything if the request itself fails -- an
    /// unknown conversation, an expired session, an unreachable server -- so
    /// those surface as ordinary errors rather than as an event inside a
    /// stream the UI has already begun rendering.
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

        let (bytes, response): (URLSession.AsyncBytes, URLResponse)
        do {
            (bytes, response) = try await session.bytes(for: request)
        } catch let error as URLError {
            // A phone that has moved off the Wi-Fi, or a Mac that went to
            // sleep. Reported as a transport failure so the UI can say
            // "NOVA server unavailable" rather than something about JSON.
            throw APIError.transport(error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.undecodable(status: 0, underlying: "Not an HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw try await Self.error(from: bytes, status: http.statusCode)
        }

        var eventName = ""
        var data = ""

        func flush() {
            if let event = Self.parse(name: eventName, data: data) {
                continuation.yield(event)
            }
            eventName = ""
            data = ""
        }

        for try await line in bytes.lines {
            if let value = line.dropPrefix("event: ") {
                // The start of the next event ends the one before it.
                //
                // SSE separates events with a blank line, and this used to
                // rely on that alone -- but `AsyncLineSequence` does not
                // yield empty lines, so the blank line never arrived and the
                // whole reply collapsed into whichever event happened to be
                // last. No deltas, no tool rows, and a `confirm` that never
                // reached the sheet.
                flush()
                eventName = value
            } else if let value = line.dropPrefix("data: ") {
                data = value
            } else if line.isEmpty {
                // Kept because it is what the protocol actually says, and
                // because a future Foundation that starts yielding blank
                // lines should not change the result. Flushing twice is
                // harmless: the second call has nothing to parse.
                flush()
            }
            // Anything else (comments, retry hints) is ignored by design.
        }

        // The last event, and the only one on a stream cut short. A stream
        // that ends without its terminator is not an error worth surfacing:
        // the server has already stored whatever it produced, and the UI
        // simply stops receiving deltas.
        flush()
    }

    /// Turn one complete SSE event into something the UI can render.
    ///
    /// Not private: the event vocabulary is a contract with the server, and
    /// the tests assert on it directly rather than through a fake socket.
    static func parse(name: String, data: String) -> ChatStreamEvent? {
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

        case "tool":
            struct Started: Decodable {
                let callId: String
                let name: String
            }
            return (try? JSONCoding.decoder.decode(Started.self, from: payload))
                .map { .toolStarted(callID: $0.callId, name: $0.name) }

        case "tool_result":
            struct Finished: Decodable {
                let callId: String
                let name: String
                let summary: String
                let isError: Bool
            }
            return (try? JSONCoding.decoder.decode(Finished.self, from: payload))
                .map {
                    .toolFinished(
                        callID: $0.callId,
                        name: $0.name,
                        summary: $0.summary,
                        isError: $0.isError
                    )
                }

        case "confirm":
            struct Confirm: Decodable {
                let callId: String
                let name: String
                let prompt: String
                let confirmationToken: String
                let arguments: [String: JSONValue]
            }
            return (try? JSONCoding.decoder.decode(Confirm.self, from: payload))
                .map {
                    .confirmationNeeded(
                        PendingConfirmation(
                            tool: $0.name,
                            prompt: $0.prompt,
                            token: $0.confirmationToken,
                            // Flattened to strings because that is the shape
                            // the invoke request takes, and every argument a
                            // tool accepts is scalar.
                            arguments: $0.arguments.compactMapValues { value in
                                value.stringValue ?? value.displayValue
                            }
                        )
                    )
                }

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
