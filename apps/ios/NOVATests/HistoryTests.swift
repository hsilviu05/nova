import Foundation
import Testing

@testable import NOVA

/// The history model against a scripted API.
@MainActor
struct HistoryTests {
    @Test("Loading fills the list and clears a stale error")
    func loadFillsList() async {
        let api = StubHistoryAPI()
        api.conversations = [.fixture(title: "Deploy checklist"), .fixture(title: "Disk usage")]
        let model = HistoryModel(api: api)

        await model.load()

        #expect(model.visible.map(\.displayTitle) == ["Deploy checklist", "Disk usage"])
        #expect(model.hasLoaded)
        #expect(!model.isEmpty)
    }

    @Test("Typing a query shows matches with their snippets, not the whole list")
    func searchShowsMatches() async {
        let api = StubHistoryAPI()
        let hit = Conversation.fixture(title: "Database choice")
        api.conversations = [hit, .fixture(title: "Unrelated")]
        api.searchResults = [ConversationSearchResult(conversation: hit, snippet: "…prefer PostgreSQL…")]
        let model = HistoryModel(api: api)
        await model.load()

        model.query = "postg"
        await model.searchAfterTyping()

        #expect(model.isSearchActive)
        #expect(model.visible.map(\.id) == [hit.id])
        #expect(model.snippet(for: hit) == "…prefer PostgreSQL…")
        #expect(api.searchQueries == ["postg"])
    }

    @Test("A blank query asks the server nothing and shows everything")
    func blankQueryIsLocal() async {
        let api = StubHistoryAPI()
        api.conversations = [.fixture(title: "One")]
        let model = HistoryModel(api: api)
        await model.load()

        model.query = "   "
        await model.searchAfterTyping()

        #expect(!model.isSearchActive)
        #expect(model.visible.count == 1)
        #expect(api.searchQueries.isEmpty)
    }

    @Test("Deleting removes the thread from both the list and the matches")
    func deleteRemovesEverywhere() async {
        let api = StubHistoryAPI()
        let doomed = Conversation.fixture(title: "Old")
        api.conversations = [doomed, .fixture(title: "Kept")]
        api.searchResults = [ConversationSearchResult(conversation: doomed, snippet: nil)]
        let model = HistoryModel(api: api)
        await model.load()
        model.query = "old"
        await model.searchAfterTyping()

        let agreed = await model.delete(doomed)

        #expect(agreed)
        #expect(api.deleted == [doomed.id])
        #expect(model.searchResults.isEmpty)
        model.query = ""
        #expect(model.visible.map(\.displayTitle) == ["Kept"])
    }

    @Test("A server refusal keeps the list and surfaces the error")
    func failedDeleteKeepsRow() async {
        let api = StubHistoryAPI()
        let kept = Conversation.fixture(title: "Kept")
        api.conversations = [kept]
        api.deleteFails = true
        let model = HistoryModel(api: api)
        await model.load()

        let agreed = await model.delete(kept)

        #expect(!agreed)
        #expect(model.visible.count == 1)
        #expect(model.error != nil)
    }
}

extension Conversation {
    static func fixture(title: String) -> Conversation {
        Conversation(
            id: UUID(),
            title: title,
            messageCount: 2,
            lastMessageAt: .now,
            createdAt: .now
        )
    }
}

/// Only the three calls the history model makes are scripted; the rest throw.
private final class StubHistoryAPI: NovaAPI, @unchecked Sendable {
    nonisolated(unsafe) var conversations: [Conversation] = []
    nonisolated(unsafe) var searchResults: [ConversationSearchResult] = []
    nonisolated(unsafe) var deleteFails = false
    nonisolated(unsafe) private(set) var searchQueries: [String] = []
    nonisolated(unsafe) private(set) var deleted: [UUID] = []

    nonisolated func conversations() async throws -> [Conversation] { conversations }
    nonisolated func searchConversations(_ query: String) async throws -> [ConversationSearchResult] {
        searchQueries.append(query)
        return searchResults
    }
    nonisolated func deleteConversation(id: UUID) async throws {
        if deleteFails { throw APIError.unauthenticated }
        deleted.append(id)
    }

    nonisolated func register(email: String, password: String, displayName: String) async throws -> AuthenticatedUser { throw APIError.unauthenticated }
    nonisolated func signIn(email: String, password: String) async throws -> AuthenticatedUser { throw APIError.unauthenticated }
    nonisolated func signOut(refreshToken: String) async throws {}
    nonisolated func currentUser() async throws -> User { throw APIError.unauthenticated }
    nonisolated func updateProfile(displayName: String?, timezone: String?) async throws -> User { throw APIError.unauthenticated }
    nonisolated func conversation(id: UUID) async throws -> ConversationDetail { throw APIError.unauthenticated }
    nonisolated func createConversation(title: String?) async throws -> Conversation { throw APIError.unauthenticated }
    nonisolated func sendMessage(_ content: String, to conversationID: UUID) async throws -> MessageExchange { throw APIError.unauthenticated }
    nonisolated func memories(category: MemoryCategory?, limit: Int, offset: Int) async throws -> MemoryPage { throw APIError.unauthenticated }
    nonisolated func searchMemories(_ query: String) async throws -> [MemorySearchResult] { throw APIError.unauthenticated }
    nonisolated func updateMemory(id: UUID, _ update: UpdateMemoryRequest) async throws -> Memory { throw APIError.unauthenticated }
    nonisolated func deleteMemory(id: UUID) async throws {}
    nonisolated func forgetEverything() async throws {}
    nonisolated func systemStatus() async throws -> SystemStatus { throw APIError.unauthenticated }
    nonisolated func activity(limit: Int) async throws -> ActivityPage { throw APIError.unauthenticated }
    nonisolated func tools() async throws -> ToolList { throw APIError.unauthenticated }
    nonisolated func invokeTool(_ name: String, arguments: [String: String], confirmationToken: String?) async throws -> ToolRunResult { throw APIError.unauthenticated }
    nonisolated func githubIntegration() async throws -> GitHubIntegration { throw APIError.unauthenticated }
    nonisolated func connectGitHub(repository: String?) async throws -> GitHubIntegrationCreated { throw APIError.unauthenticated }
    nonisolated func updateGitHubIntegration(repository: String?, enabled: Bool?) async throws -> GitHubIntegration { throw APIError.unauthenticated }
    nonisolated func disconnectGitHub() async throws {}
}
