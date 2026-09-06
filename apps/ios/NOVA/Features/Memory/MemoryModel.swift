import Foundation
import Observation

/// Drives the memory screen: what NOVA knows, and changing it.
///
/// `filter` and `query` are plain observable properties with no observers on
/// them. Reloading and searching are driven from the view with `.task(id:)`,
/// which cancels the previous run when the value changes -- so a query typed
/// letter by letter debounces itself, and the last response to arrive is the
/// last one asked for rather than whichever was quickest.
@MainActor
@Observable
final class MemoryModel {
    private(set) var memories: [Memory] = []
    private(set) var total = 0
    private(set) var searchResults: [MemorySearchResult] = []
    private(set) var isLoading = false
    private(set) var isSearching = false
    private(set) var hasLoaded = false
    var error: APIError?

    /// Nil means every category.
    var filter: MemoryCategory?
    var query = ""

    private let api: any NovaAPI

    init(api: any NovaAPI) {
        self.api = api
    }

    var isFirstLoad: Bool { !hasLoaded && isLoading }

    /// Empty only when there is genuinely nothing, not when a filter hid it.
    var isEmpty: Bool { hasLoaded && memories.isEmpty && filter == nil }

    var isSearchActive: Bool {
        !query.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// What the list shows: search results while searching, the browsed list
    /// otherwise. One property, so the view has no branching state of its own
    /// to fall out of step with this.
    var visible: [Memory] {
        isSearchActive ? searchResults.map(\.memory) : memories
    }

    func similarity(for memory: Memory) -> Double? {
        searchResults.first { $0.memory.id == memory.id }?.similarity
    }

    // MARK: - Loading

    func load() async {
        isLoading = true
        defer {
            isLoading = false
            hasLoaded = true
        }

        do {
            let page = try await api.memories(category: filter, limit: 100, offset: 0)
            memories = page.items
            total = page.total
            error = nil
        } catch {
            // Keep what is on screen: a failed refresh should not blank a
            // working list.
            report(error)
        }
    }

    /// Search, after waiting long enough that a typed word is one request.
    ///
    /// The wait is here rather than in the view because cancellation is what
    /// makes it a debounce, and `.task(id:)` cancels this whole call.
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
            searchResults = try await api.searchMemories(trimmed)
            error = nil
        } catch is CancellationError {
            return
        } catch {
            report(error)
        }
    }

    // MARK: - Changing

    func update(_ memory: Memory, content: String, category: MemoryCategory) async {
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        do {
            let updated = try await api.updateMemory(
                id: memory.id,
                UpdateMemoryRequest(
                    // Only what changed. Sending the same text back would
                    // re-embed it and promote its confidence to a human
                    // correction that was never made.
                    content: trimmed == memory.content ? nil : trimmed,
                    category: category == memory.category ? nil : category,
                    importance: nil
                )
            )
            replace(updated)
            error = nil
        } catch {
            report(error)
        }
    }

    func delete(_ memory: Memory) async {
        // Removed from the list first: waiting on the round trip makes a
        // deliberate action feel unresponsive. A failure puts it back.
        let previous = memories
        let previousTotal = total
        memories.removeAll { $0.id == memory.id }
        searchResults.removeAll { $0.memory.id == memory.id }
        total = max(0, total - 1)

        do {
            try await api.deleteMemory(id: memory.id)
            error = nil
        } catch {
            memories = previous
            total = previousTotal
            report(error)
        }
    }

    func forgetEverything() async {
        do {
            try await api.forgetEverything()
            memories = []
            searchResults = []
            total = 0
            error = nil
        } catch {
            report(error)
        }
    }

    // MARK: - Internals

    private func replace(_ memory: Memory) {
        if let index = memories.firstIndex(where: { $0.id == memory.id }) {
            memories[index] = memory
        }
        if let index = searchResults.firstIndex(where: { $0.memory.id == memory.id }) {
            searchResults[index] = MemorySearchResult(
                memory: memory, similarity: searchResults[index].similarity
            )
        }
    }

    private func report(_ error: any Error) {
        self.error = (error as? APIError) ?? .undecodable(status: 0, underlying: "\(error)")
    }
}
