import Foundation

/// One thing NOVA remembers about its owner.
///
/// Deliberately readable rather than internal-looking: this is shown to the
/// person it is about, and the point of the screen is that they can tell
/// what NOVA believes and change it.
struct Memory: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let content: String
    let category: MemoryCategory
    /// How much this shapes replies, 0-1.
    let importance: Double
    /// How sure NOVA is that it is true, 0-1. A correction sets it to 1.
    let confidence: Double
    /// Where it came from. Nil once that conversation has been deleted --
    /// the memory outlives the transcript.
    let sourceConversationId: UUID?
    let recallCount: Int
    let lastRecalledAt: Date?
    let createdAt: Date

    /// Never recalled reads differently from recalled once; the distinction
    /// is worth a word rather than a zero.
    var recallDescription: String {
        switch recallCount {
        case 0: "Never used"
        case 1: "Used once"
        default: "Used \(recallCount) times"
        }
    }
}

/// The closed taxonomy the API accepts.
///
/// `unknown` exists so a category added server-side does not stop the whole
/// list decoding -- a memory shown under a vague label is far better than a
/// screen that fails to load.
enum MemoryCategory: String, Codable, CaseIterable, Identifiable, Sendable {
    case preference, fact, routine, relationship, project, event
    case unknown

    var id: String { rawValue }

    /// Categories a person can choose. `unknown` is never offered.
    static var selectable: [MemoryCategory] {
        allCases.filter { $0 != .unknown }
    }

    init(from decoder: any Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = MemoryCategory(rawValue: raw) ?? .unknown
    }

    var label: String {
        switch self {
        case .preference: "Preference"
        case .fact: "Fact"
        case .routine: "Routine"
        case .relationship: "Person"
        case .project: "Project"
        case .event: "Event"
        case .unknown: "Other"
        }
    }

    var symbol: String {
        switch self {
        case .preference: "heart"
        case .fact: "text.book.closed"
        case .routine: "repeat"
        case .relationship: "person.2"
        case .project: "hammer"
        case .event: "calendar"
        case .unknown: "questionmark.circle"
        }
    }
}

/// A page of memories with the total, so a count can be shown without
/// fetching everything.
struct MemoryPage: Decodable, Sendable {
    let items: [Memory]
    let total: Int
}

/// A memory with how well it matched a search.
struct MemorySearchResult: Decodable, Identifiable, Sendable {
    let memory: Memory
    /// 1.0 is identical, 0.0 unrelated.
    let similarity: Double

    var id: UUID { memory.id }
}

// MARK: - Requests

/// A correction. Every field is optional: omitted means unchanged, so the app
/// sends only what was edited.
struct UpdateMemoryRequest: Encodable, Sendable {
    var content: String?
    var category: MemoryCategory?
    var importance: Double?
}
