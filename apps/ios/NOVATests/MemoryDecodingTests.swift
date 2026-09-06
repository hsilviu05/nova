import Foundation
import Testing

@testable import NOVA

/// Memory payloads, captured verbatim from a running NOVA API.
///
/// Recorded rather than invented, for the same reason as `DecodingTests`: an
/// invented fixture proves the decoder agrees with whoever wrote the fixture,
/// which is not the question.
struct MemoryDecodingTests {
    /// A real `GET /api/v1/memories` response.
    private let pageJSON = """
    {
      "items": [
        {
          "id": "337d7aca-c12c-4f26-863d-1e409edba726",
          "content": "Drinks coffee every morning before work",
          "category": "routine",
          "importance": 0.8,
          "confidence": 0.9,
          "source_conversation_id": "2fd5c1ec-26fd-4421-9e16-8463ea504e4f",
          "recall_count": 0,
          "last_recalled_at": null,
          "created_at": "2026-09-06T16:46:16.866272Z"
        }
      ],
      "total": 1
    }
    """

    /// A real `GET /api/v1/memories/search` response.
    private let searchJSON = """
    [
      {
        "memory": {
          "id": "337d7aca-c12c-4f26-863d-1e409edba726",
          "content": "Drinks coffee every morning before work",
          "category": "routine",
          "importance": 0.8,
          "confidence": 0.9,
          "source_conversation_id": "2fd5c1ec-26fd-4421-9e16-8463ea504e4f",
          "recall_count": 1,
          "last_recalled_at": "2026-09-06T16:46:16.977355Z",
          "created_at": "2026-09-06T16:46:16.866272Z"
        },
        "similarity": 0.3560541484388208
      }
    ]
    """

    @Test("Decodes a page of memories")
    func page() throws {
        let page = try JSONCoding.decoder.decode(
            MemoryPage.self, from: Data(pageJSON.utf8)
        )

        #expect(page.total == 1)
        let memory = try #require(page.items.first)
        #expect(memory.content == "Drinks coffee every morning before work")
        #expect(memory.category == .routine)
        #expect(memory.importance == 0.8)
        #expect(memory.sourceConversationId != nil)
        #expect(memory.lastRecalledAt == nil)
    }

    @Test("Decodes search results with their scores")
    func search() throws {
        let results = try JSONCoding.decoder.decode(
            [MemorySearchResult].self, from: Data(searchJSON.utf8)
        )

        let first = try #require(results.first)
        #expect(first.similarity > 0.3)
        #expect(first.memory.recallCount == 1)
        #expect(first.memory.lastRecalledAt != nil)
        // Identity comes from the memory, so a result and the row it wraps
        // are the same thing to SwiftUI.
        #expect(first.id == first.memory.id)
    }

    @Test("A category the server adds later does not break the list")
    func unknownCategory() throws {
        // The whole screen failing to load because one row has a category
        // this build has never heard of would be a bad trade.
        let json = """
        {
          "id": "337d7aca-c12c-4f26-863d-1e409edba726",
          "content": "Something new",
          "category": "aspiration",
          "importance": 0.5,
          "confidence": 0.5,
          "source_conversation_id": null,
          "recall_count": 0,
          "last_recalled_at": null,
          "created_at": "2026-09-06T16:46:16.866272Z"
        }
        """

        let memory = try JSONCoding.decoder.decode(Memory.self, from: Data(json.utf8))
        #expect(memory.category == .unknown)
        #expect(memory.content == "Something new")
    }

    @Test("Unknown is never offered as a choice")
    func unknownIsNotSelectable() {
        // It is the app's fallback, not a value the API accepts; sending it
        // back would be a 422.
        #expect(!MemoryCategory.selectable.contains(.unknown))
        #expect(MemoryCategory.selectable.count == MemoryCategory.allCases.count - 1)
    }

    @Test("Recall counts read as words, not as a bare zero")
    func recallDescription() {
        func memory(recalled: Int) -> Memory {
            Memory(
                id: UUID(),
                content: "x",
                category: .fact,
                importance: 0.5,
                confidence: 0.5,
                sourceConversationId: nil,
                recallCount: recalled,
                lastRecalledAt: nil,
                createdAt: .now
            )
        }

        #expect(memory(recalled: 0).recallDescription == "Never used")
        #expect(memory(recalled: 1).recallDescription == "Used once")
        #expect(memory(recalled: 4).recallDescription == "Used 4 times")
    }

    @Test("A correction sends only what changed")
    func updateEncodesOnlyChanges() throws {
        let update = UpdateMemoryRequest(
            content: "Drinks peppermint tea", category: nil, importance: nil
        )
        let encoded = try JSONCoding.encoder.encode(update)
        let object = try #require(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )

        #expect(object["content"] as? String == "Drinks peppermint tea")
        // An omitted field must not become an explicit null that clears the
        // value server-side.
        #expect(object["importance"] == nil)
    }
}
