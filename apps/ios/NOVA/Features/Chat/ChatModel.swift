import Foundation
import Observation

/// Drives one conversation.
@MainActor
@Observable
final class ChatModel {
    /// What NOVA is doing, which the UI shows rather than a generic spinner.
    enum Activity: Equatable {
        case idle
        case thinking
        case speaking
    }

    private(set) var messages: [ChatMessage] = []
    private(set) var activity: Activity = .idle
    private(set) var conversationID: UUID?
    private(set) var isLoading = false
    var error: APIError?

    /// The id of the assistant turn currently being assembled, so deltas
    /// append to it rather than creating a bubble per fragment.
    private var streamingID: UUID?
    private var streamTask: Task<Void, Never>?

    private let api: any NovaAPI
    private let stream: ChatStreamClient

    init(api: any NovaAPI, stream: ChatStreamClient, conversationID: UUID? = nil) {
        self.api = api
        self.stream = stream
        self.conversationID = conversationID
    }

    var canSend: Bool {
        activity == .idle && !isLoading
    }

    // MARK: - Loading

    /// Open the most recent conversation, or start one.
    func load() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }

        do {
            if let id = conversationID {
                try await loadDetail(id)
                return
            }

            // Continue where the owner left off rather than opening a fresh
            // thread every launch -- a companion with no memory of the last
            // exchange is a search box.
            if let existing = try await api.conversations().first {
                conversationID = existing.id
                try await loadDetail(existing.id)
            } else {
                conversationID = try await api.createConversation(title: nil).id
                messages = []
            }
            error = nil
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    private func loadDetail(_ id: UUID) async throws {
        messages = try await api.conversation(id: id).messages
        error = nil
    }

    /// Begin a new thread, leaving the previous one intact.
    func startNewConversation() async {
        cancel()
        do {
            conversationID = try await api.createConversation(title: nil).id
            messages = []
            error = nil
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    // MARK: - Sending

    func send(_ text: String) {
        let content = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !content.isEmpty, canSend, let id = conversationID else { return }

        // Shown immediately so the input clears and the bubble appears
        // without waiting for a round trip; the server echoes the stored
        // turn and it is replaced below.
        let optimistic = ChatMessage.pending(content)
        messages.append(optimistic)
        activity = .thinking
        error = nil

        streamTask = Task { [weak self] in
            await self?.consume(content, id, replacing: optimistic.id)
        }
    }

    private func consume(
        _ content: String, _ conversationID: UUID, replacing optimisticID: UUID
    ) async {
        do {
            for try await event in stream.send(content, to: conversationID) {
                switch event {
                case let .message(stored):
                    replace(optimisticID, with: stored)

                case let .delta(text):
                    appendDelta(text)

                case .done:
                    finish()

                case let .failed(code, message):
                    error = .api(
                        status: 502,
                        envelope: .init(
                            code: code, message: message, requestId: nil, details: nil
                        )
                    )
                    finish()
                }
            }
            // A stream ending without `done` was cut short. Whatever arrived
            // is kept: the server stored the same partial reply.
            finish()
        } catch let apiError as APIError {
            error = apiError
            rollback(optimisticID)
        } catch is CancellationError {
            finish()
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
            rollback(optimisticID)
        }
    }

    /// Stop the reply. The server keeps what it already produced.
    func cancel() {
        streamTask?.cancel()
        streamTask = nil
        finish()
    }

    // MARK: - Transcript maintenance

    private func replace(_ id: UUID, with stored: ChatMessage) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index] = stored
    }

    private func appendDelta(_ text: String) {
        activity = .speaking

        if let id = streamingID,
           let index = messages.firstIndex(where: { $0.id == id }) {
            messages[index] = .streaming(messages[index].content + text, id: id)
        } else {
            let id = UUID()
            streamingID = id
            messages.append(.streaming(text, id: id))
        }
    }

    private func finish() {
        streamingID = nil
        activity = .idle
    }

    /// Remove the optimistic turn when the send never reached the server.
    ///
    /// Only safe because this runs when nothing was persisted; a failure
    /// *after* the user turn was stored keeps it, since the server has it.
    private func rollback(_ id: UUID) {
        messages.removeAll { $0.id == id }
        finish()
    }
}
