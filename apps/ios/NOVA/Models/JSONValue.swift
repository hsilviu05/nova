import Foundation

/// A decoded JSON value of unknown shape.
///
/// Two places need one. A tool's `input_schema` is JSON Schema, which the
/// Tools screen reads field names out of and otherwise treats as opaque. A
/// tool's `data` is whatever that tool returns — a container list, a disk
/// reading, a health report — and a Swift type per tool would mean shipping
/// an app update every time the server grows one.
///
/// Deliberately small: it exists to display and inspect values, not to be a
/// general JSON library.
enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()

        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            // Before Double: JSONDecoder would otherwise read true as 1.
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "Unsupported JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()

        switch self {
        case let .string(value): try container.encode(value)
        case let .number(value): try container.encode(value)
        case let .bool(value): try container.encode(value)
        case let .object(value): try container.encode(value)
        case let .array(value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    /// A short form suitable for a label.
    var displayValue: String {
        switch self {
        case let .string(value): value
        case let .number(value):
            value == value.rounded()
                ? String(Int(value))
                : String(format: "%.2f", value)
        case let .bool(value): value ? "yes" : "no"
        case let .object(value): "{\(value.count) fields}"
        case let .array(value): "[\(value.count)]"
        case .null: "—"
        }
    }

    var stringValue: String? {
        if case let .string(value) = self { return value }
        return nil
    }
}
