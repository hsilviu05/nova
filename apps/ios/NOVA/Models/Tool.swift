import Foundation

/// One thing NOVA can do on the machine it runs on.
///
/// The permission is here, unlike in what the model is shown: somebody about
/// to press a button is entitled to know whether it changes anything.
struct NovaTool: Decodable, Equatable, Sendable, Identifiable {
    enum Permission: String, Decodable, Sendable {
        case read, write, destructive
        /// A level this build has not heard of. Treated as the most
        /// dangerous thing it could be — an unknown permission is not a
        /// reason to relax, and a newer server may have added one.
        case unknown

        init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Permission(rawValue: raw) ?? .unknown
        }

        var changesThings: Bool { self != .read }
    }

    let name: String
    let description: String
    let group: String
    let permission: Permission
    let requiresConfirmation: Bool
    /// JSON Schema for the arguments. Kept as an opaque value: the Tools
    /// screen reads the field names from it, and nothing else needs it.
    let inputSchema: JSONValue

    var id: String { name }

    /// The argument names, in schema order where the server gave one.
    var argumentNames: [String] {
        guard case let .object(schema) = inputSchema,
              case let .object(properties)? = schema["properties"]
        else { return [] }

        let required: Set<String>
        if case let .array(names)? = schema["required"] {
            required = Set(names.compactMap(\.stringValue))
        } else {
            required = []
        }

        // Required first, then the rest, each alphabetically: a stable order
        // the form can render without the server having to promise one.
        let all = properties.keys.sorted()
        return all.filter(required.contains) + all.filter { !required.contains($0) }
    }

    func isRequired(_ argument: String) -> Bool {
        guard case let .object(schema) = inputSchema,
              case let .array(names)? = schema["required"]
        else { return false }
        return names.compactMap(\.stringValue).contains(argument)
    }
}

struct ToolList: Decodable, Equatable, Sendable {
    let items: [NovaTool]
    /// Whether arbitrary command execution is turned on. Usually false, and
    /// the Settings screen says so out loud when it is not.
    let shellEnabled: Bool
}

/// What a tool produced.
struct ToolRunResult: Decodable, Equatable, Sendable {
    let tool: String
    let content: String
    let data: JSONValue
    let isError: Bool
    /// Output was cut. Shown, never hidden: a truncated listing that looks
    /// complete is worse than no listing.
    let truncated: Bool
    let durationMs: Int
}

struct InvokeToolRequest: Encodable, Sendable {
    let name: String
    let arguments: [String: String]
    /// Present on the second call, after the person has seen what would
    /// happen. Absent on the first, which is what makes the server answer
    /// with a confirmation rather than an action.
    var confirmationToken: String?
}

/// A destructive call waiting for a person to agree to it.
///
/// Built from the 409 body, or from the `confirm` event in a reply stream.
/// Both carry the same fields, because they are the same handshake reached
/// two different ways.
struct PendingConfirmation: Equatable, Sendable, Identifiable {
    let tool: String
    let prompt: String
    let token: String
    var arguments: [String: String] = [:]

    var id: String { token }

    /// Read a confirmation out of a 409 error envelope.
    init?(_ error: APIError) {
        guard case let .api(_, envelope) = error,
              envelope.code == "tool_confirmation_required",
              let details = envelope.details,
              let token = details.confirmationToken,
              let prompt = details.prompt,
              let tool = details.tool
        else { return nil }

        self.tool = tool
        self.prompt = prompt
        self.token = token
    }

    init(tool: String, prompt: String, token: String, arguments: [String: String] = [:]) {
        self.tool = tool
        self.prompt = prompt
        self.token = token
        self.arguments = arguments
    }
}
