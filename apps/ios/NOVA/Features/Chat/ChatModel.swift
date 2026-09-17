import Foundation
import Observation

/// One item in the transcript.
///
/// Tool activity is part of the transcript rather than an overlay, because it
/// is part of what happened: "I checked SnapWorth" belongs in the thread
/// between the question and the answer, and a spinner that disappears leaves
/// no record that anything was checked at all.
enum TranscriptItem: Identifiable, Equatable, Sendable {
    case message(ChatMessage)
    case tool(ToolActivity)

    var id: String {
        switch self {
        case let .message(message): message.id.uuidString
        case let .tool(activity): activity.callID
        }
    }
}

/// A tool NOVA ran, or is running, mid-reply.
struct ToolActivity: Identifiable, Equatable, Sendable {
    let callID: String
    let name: String
    var summary: String?
    var isError = false

    var id: String { callID }
    var isRunning: Bool { summary == nil }
}

/// Drives one conversation.
@MainActor
@Observable
final class ChatModel {
    /// What NOVA is doing, which the UI shows rather than a generic spinner.
    enum Activity: Equatable {
        case idle
        case thinking
        case usingTool(String)
        case speaking
    }

    private(set) var transcript: [TranscriptItem] = []
    private(set) var activity: Activity = .idle
    private(set) var conversationID: UUID?
    private(set) var isLoading = false

    /// A destructive action NOVA has proposed. The app raises the same sheet
    /// the Tools screen does, so the moment of consent looks identical
    /// wherever it is reached from.
    var pendingConfirmation: PendingConfirmation?
    var error: APIError?

    /// The last thing sent, so a failed send can be retried without making
    /// someone type it again.
    private(set) var lastSent: String?

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

    var isStreaming: Bool {
        activity != .idle
    }

    var canRetry: Bool {
        lastSent != nil && error != nil && canSend
    }

    /// The most recent thing NOVA said in full, for reading aloud.
    var lastAssistantText: String? {
        for item in transcript.reversed() {
            if case let .message(message) = item, message.isFromNova {
                return message.content
            }
        }
        return nil
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
            // thread every launch -- a terminal with no memory of the last
            // exchange is a search box.
            if let existing = try await api.conversations().first {
                conversationID = existing.id
                try await loadDetail(existing.id)
            } else {
                conversationID = try await api.createConversation(title: nil).id
                transcript = []
            }
            error = nil
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    private func loadDetail(_ id: UUID) async throws {
        // Tool activity is not persisted as messages, so a reloaded thread
        // shows what was said without the machinery. That is the right
        // trade: the audit log is where "what did NOVA run" is answered
        // permanently, and it is a screen of its own.
        transcript = try await api.conversation(id: id).messages.map(TranscriptItem.message)
        error = nil
    }

    /// Switch to an existing thread, chosen from history.
    func open(_ id: UUID) async {
        guard id != conversationID else { return }
        cancel()
        conversationID = id
        lastSent = nil
        do {
            try await loadDetail(id)
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    /// Forget a thread that was just deleted. If it was the open one, the
    /// next send needs a thread to go into, so start a fresh one now rather
    /// than failing later with a not-found.
    func conversationWasDeleted(_ id: UUID) async {
        guard id == conversationID else { return }
        await startNewConversation()
    }

    /// Begin a new thread, leaving the previous one intact.
    func startNewConversation() async {
        cancel()
        do {
            conversationID = try await api.createConversation(title: nil).id
            transcript = []
            lastSent = nil
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
        transcript.append(.message(optimistic))
        activity = .thinking
        lastSent = content
        error = nil

        streamTask = Task { [weak self] in
            await self?.consume(content, id, replacing: optimistic.id)
        }
    }

    /// Send the last message again after a failure.
    func retry() {
        guard let content = lastSent, canSend else { return }
        // Drop the failed turn first: the server never stored it, so leaving
        // it would show the question twice.
        if case let .message(last)? = transcript.last, last.role == .user,
           last.content == content {
            transcript.removeLast()
        }
        error = nil
        send(content)
    }

    private func consume(
        _ content: String, _ conversationID: UUID, replacing optimisticID: UUID
    ) async {
        do {
            for try await event in stream.send(content, to: conversationID) {
                if case let .message(stored) = event {
                    replace(optimisticID, with: stored)
                } else {
                    apply(event)
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

    /// Fold one stream event into the transcript.
    ///
    /// Separated from the loop above so it can be exercised directly. What
    /// is worth testing here is how events assemble -- deltas joining into
    /// one bubble, a tool splitting them, a proposal becoming a sheet rather
    /// than an action -- and none of that is a question about transport.
    ///
    /// `.message` is handled by the caller, which is the only place that
    /// knows which optimistic turn it replaces.
    func apply(_ event: ChatStreamEvent) {
        switch event {
        case let .message(stored):
            transcript.append(.message(stored))

        case let .delta(text):
            appendDelta(text)

        case let .toolStarted(callID, name):
            activity = .usingTool(name)
            // Closes the current bubble, so anything NOVA says after the
            // tool lands in a new one below it and the order reads the way
            // it happened.
            streamingID = nil
            transcript.append(.tool(ToolActivity(callID: callID, name: name)))

        case let .toolFinished(callID, _, summary, isError):
            finishTool(callID, summary: summary, isError: isError)

        case let .confirmationNeeded(confirmation):
            pendingConfirmation = confirmation

        case .done:
            finish()

        case let .failed(code, message):
            error = .api(
                status: 502,
                envelope: .init(
                    code: code,
                    message: message,
                    requestId: nil,
                    details: nil
                )
            )
            finish()
        }
    }

    /// Stop the reply. The server keeps what it already produced.
    func cancel() {
        streamTask?.cancel()
        streamTask = nil
        finish()
    }

    // MARK: - Confirmations

    /// Go ahead with what NOVA proposed.
    ///
    /// Run through the ordinary tool endpoint rather than through the
    /// conversation: approving an action is a direct instruction from a
    /// person, and it should be recorded as one. The result is posted back
    /// into the thread as a message so the transcript stays honest about
    /// what happened.
    func confirm() async {
        guard let confirmation = pendingConfirmation else { return }
        pendingConfirmation = nil
        activity = .usingTool(confirmation.tool)
        defer { activity = .idle }

        let callID = "confirmed-\(confirmation.token.prefix(8))"
        transcript.append(.tool(ToolActivity(callID: callID, name: confirmation.tool)))

        do {
            let result = try await api.invokeTool(
                confirmation.tool,
                arguments: confirmation.arguments,
                confirmationToken: confirmation.token
            )
            finishTool(
                callID,
                summary: result.content.split(separator: "\n").first.map(String.init)
                    ?? "Done",
                isError: result.isError
            )
        } catch let apiError as APIError {
            finishTool(callID, summary: apiError.userMessage, isError: true)
            error = apiError
        } catch {
            finishTool(callID, summary: "Failed", isError: true)
        }
    }

    func declineConfirmation() {
        pendingConfirmation = nil
    }

    // MARK: - Transcript maintenance

    private func replace(_ id: UUID, with stored: ChatMessage) {
        guard let index = index(of: id) else { return }
        transcript[index] = .message(stored)
    }

    private func appendDelta(_ text: String) {
        activity = .speaking

        if let id = streamingID, let index = index(of: id),
           case let .message(existing) = transcript[index] {
            transcript[index] = .message(.streaming(existing.content + text, id: id))
        } else {
            let id = UUID()
            streamingID = id
            transcript.append(.message(.streaming(text, id: id)))
        }
    }

    private func finishTool(_ callID: String, summary: String, isError: Bool) {
        guard let index = transcript.firstIndex(where: { $0.id == callID }),
              case let .tool(existing) = transcript[index]
        else { return }

        var finished = existing
        finished.summary = summary
        finished.isError = isError
        transcript[index] = .tool(finished)
    }

    private func index(of id: UUID) -> Int? {
        transcript.firstIndex { $0.id == id.uuidString }
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
        transcript.removeAll { $0.id == id.uuidString }
        finish()
    }
}
