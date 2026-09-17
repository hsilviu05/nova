import Foundation
import Observation

/// Past conversations, and searching through them.
///
/// Same shape as `MemoryModel`: `query` is a plain observable property, and
/// the view drives `searchAfterTyping()` from `.task(id:)`, which cancels the
/// previous run on every keystroke. That is what makes typing a word cost one
/// request instead of one per letter, without a timer of its own.
@MainActor
@Observable
final class HistoryModel {
    private(set) var conversations: [Conversation] = []
    private(set) var searchResults: [ConversationSearchResult] = []
    private(set) var isLoading = false
    private(set) var isSearching = false
    private(set) var hasLoaded = false
    var error: APIError?

    var query = ""

    private let api: any NovaAPI

    init(api: any NovaAPI) {
        self.api = api
    }

    var isSearchActive: Bool {
        !query.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// What the list shows: matches while searching, everything otherwise.
    var visible: [Conversation] {
        isSearchActive ? searchResults.map(\.conversation) : conversations
    }

    func snippet(for conversation: Conversation) -> String? {
        searchResults.first { $0.conversation.id == conversation.id }?.snippet
    }

    var isEmpty: Bool { hasLoaded && conversations.isEmpty }

    // MARK: - Loading

    func load() async {
        isLoading = true
        defer {
            isLoading = false
            hasLoaded = true
        }
        do {
            conversations = try await api.conversations()
            error = nil
        } catch {
            report(error)
        }
    }

    /// Search, once typing has paused long enough to be a word.
    func searchAfterTyping() async {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            searchResults = []
            return
        }

        do {
            try await Task.sleep(for: .milliseconds(250))
        } catch {
            return  // Superseded by a newer keystroke.
        }

        isSearching = true
        defer { isSearching = false }

        do {
            searchResults = try await api.searchConversations(trimmed)
            error = nil
        } catch is CancellationError {
            return
        } catch {
            report(error)
        }
    }

    // MARK: - Deleting

    /// Remove a thread. Returns whether the server agreed, so the caller can
    /// decide what to do about the thread that is open.
    @discardableResult
    func delete(_ conversation: Conversation) async -> Bool {
        do {
            try await api.deleteConversation(id: conversation.id)
            conversations.removeAll { $0.id == conversation.id }
            searchResults.removeAll { $0.conversation.id == conversation.id }
            error = nil
            return true
        } catch {
            report(error)
            return false
        }
    }

    private func report(_ error: any Error) {
        if let apiError = error as? APIError {
            self.error = apiError
        } else {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }
}
