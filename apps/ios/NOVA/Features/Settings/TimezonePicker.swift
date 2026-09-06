import SwiftUI

/// Choosing the timezone every analytics figure is bucketed in.
///
/// A searchable list of IANA identifiers rather than a free text field: the
/// API validates the name against the tz database and rejects anything it
/// cannot resolve, so offering somewhere to type "GMT+2" would only produce a
/// 422 the person cannot act on.
struct TimezonePicker: View {
    let current: String

    @Environment(SessionStore.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    /// Everything the device's own tz database knows, which is the same
    /// database the server validates against.
    private var identifiers: [String] {
        TimeZone.knownTimeZoneIdentifiers.sorted()
    }

    private var matches: [String] {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return identifiers }
        return identifiers.filter {
            $0.localizedCaseInsensitiveContains(trimmed)
        }
    }

    var body: some View {
        List {
            if !query.isEmpty && matches.isEmpty {
                Text("No time zone matching that.")
                    .foregroundStyle(.secondary)
            }

            ForEach(matches, id: \.self) { identifier in
                Button {
                    Task {
                        await session.updateTimezone(identifier)
                        dismiss()
                    }
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(identifier.replacingOccurrences(of: "_", with: " "))
                                .foregroundStyle(.primary)
                            if let offset = offsetLabel(for: identifier) {
                                Text(offset)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        if identifier == current {
                            Image(systemName: "checkmark")
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                }
            }
        }
        .searchable(text: $query, prompt: "Search time zones")
        .navigationTitle("Time zone")
        .navigationBarTitleDisplayMode(.inline)
    }

    /// The offset in effect right now.
    ///
    /// Shown as a hint only. The stored value is the identifier, never the
    /// offset: an offset is wrong for half the year everywhere that observes
    /// daylight saving, which is the whole reason the server wants a name.
    private func offsetLabel(for identifier: String) -> String? {
        guard let zone = TimeZone(identifier: identifier) else { return nil }
        let seconds = zone.secondsFromGMT()
        let sign = seconds < 0 ? "-" : "+"
        let hours = abs(seconds) / 3600
        let minutes = (abs(seconds) % 3600) / 60
        return String(format: "GMT%@%02d:%02d", sign, hours, minutes)
    }
}
