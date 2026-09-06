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

/// ISO-8601 parsing across the three shapes this API actually emits.
///
/// 1. Microsecond timestamps — `2026-09-06T15:42:28.579713Z` — which the
///    built-in `.iso8601` strategy does not parse at all.
/// 2. Timestamps landing exactly on a whole second, which serialise without
///    a fractional part. Rare, and a crash the one time it happens.
/// 3. Bare calendar dates — `2026-08-17` — which analytics uses for daily
///    buckets, because a local calendar day is a date and not an instant.
///
/// The third is easy to miss and expensive: `presence_by_day` sits inside the
/// analytics response, so failing on it does not drop a field, it fails the
/// whole decode and blanks the insights screen.
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

    /// Date-only, resolved at UTC midnight.
    ///
    /// The server has already done the timezone work: the value is the local
    /// calendar date the events fell on. Re-interpreting it in the phone's
    /// zone would shift a whole day's bar to the one either side of it.
    private static let dateOnly: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withFullDate, .withDashSeparatorInDate]
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }()

    static func date(from string: String) -> Date? {
        withFractional.date(from: string)
            ?? withoutFractional.date(from: string)
            ?? dateOnly.date(from: string)
    }

    static func string(from date: Date) -> String {
        withFractional.string(from: date)
    }
}
