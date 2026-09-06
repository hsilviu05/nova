import Foundation

/// JSON coders configured for the NOVA API.
///
/// Two things the defaults get wrong for this API:
///
/// 1. The API is snake_case, so keys are converted rather than every model
///    carrying a `CodingKeys` enum.
/// 2. Timestamps carry fractional seconds — `2026-09-06T15:42:28.579713Z` —
///    and `JSONDecoder.DateDecodingStrategy.iso8601` does **not** parse them.
///    Using it would fail on essentially every response.
enum JSONCoding {
    static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)

            guard let date = ISO8601.date(from: raw) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Not an ISO-8601 timestamp: \(raw)"
                )
            }
            return date
        }
        return decoder
    }()

    static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(ISO8601.string(from: date))
        }
        return encoder
    }()
}

/// ISO-8601 parsing that tolerates a missing fractional part.
///
/// The API emits microseconds, but a timestamp landing exactly on a whole
/// second serialises without them — rare, and a crash the one time it
/// happens. Both forms are accepted.
enum ISO8601 {
    private static let withFractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let withoutFractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static func date(from string: String) -> Date? {
        withFractional.date(from: string) ?? withoutFractional.date(from: string)
    }

    static func string(from date: Date) -> String {
        withFractional.string(from: date)
    }
}
