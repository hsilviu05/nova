import Foundation

/// A thread of messages between the owner and NOVA.
struct Conversation: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    /// Derived server-side from the first message rather than asked for.
    let title: String?
    let messageCount: Int
    let lastMessageAt: Date?
    let createdAt: Date

    var displayTitle: String {
        title ?? "New conversation"
    }
}

/// A conversation with its messages, oldest first.
struct ConversationDetail: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let title: String?
    let messageCount: Int
    let lastMessageAt: Date?
    let createdAt: Date
    let messages: [ChatMessage]
}

/// One turn.
struct ChatMessage: Codable, Identifiable, Equatable, Sendable {
    enum Role: String, Codable, Sendable {
        case user, assistant
    }

    let id: UUID
    let role: Role
    let content: String
    let createdAt: Date
    let model: String?
    let latencyMs: Int?

    var isFromNova: Bool { role == .assistant }
}

extension ChatMessage {
    /// A message shown before the server has confirmed it.
    ///
    /// Given a client-side id so SwiftUI can animate it in immediately; the
    /// server echoes the stored turn as the stream's first event and the
    /// optimistic copy is replaced by it.
    static func pending(_ content: String) -> ChatMessage {
        ChatMessage(
            id: UUID(),
            role: .user,
            content: content,
            createdAt: .now,
            model: nil,
            latencyMs: nil
        )
    }

    /// The assistant turn being assembled from stream deltas.
    static func streaming(_ text: String, id: UUID) -> ChatMessage {
        ChatMessage(
            id: id,
            role: .assistant,
            content: text,
            createdAt: .now,
            model: nil,
            latencyMs: nil
        )
    }
}

// MARK: - Requests

struct CreateConversationRequest: Encodable, Sendable {
    let title: String?
}

struct SendMessageRequest: Encodable, Sendable {
    let content: String
}

/// Both halves of a completed non-streaming exchange.
struct MessageExchange: Decodable, Sendable {
    let userMessage: ChatMessage
    let assistantMessage: ChatMessage
}
