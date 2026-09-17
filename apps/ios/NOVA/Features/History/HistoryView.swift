import SwiftUI

/// Every past conversation, searchable by title or by anything said in it.
///
/// Presented as a sheet over the chat rather than as a tab: it is a way of
/// choosing what the chat shows, not a destination of its own.
struct HistoryView: View {
    let api: any NovaAPI
    /// The thread the chat currently shows, marked in the list.
    let current: UUID?
    let onOpen: (UUID) -> Void
    let onDeleted: (UUID) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var model: HistoryModel?

    var body: some View {
        NavigationStack {
            Group {
                if let model {
                    HistoryList(model: model, current: current, onOpen: onOpen, onDeleted: onDeleted)
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .navigationTitle("History")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .task {
            if model == nil { model = HistoryModel(api: api) }
        }
    }
}

private struct HistoryList: View {
    @Bindable var model: HistoryModel
    let current: UUID?
    let onOpen: (UUID) -> Void
    let onDeleted: (UUID) -> Void

    var body: some View {
        List {
            if let error = model.error {
                Section {
                    Label(error.userMessage, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.secondary)
                }
            }

            Section {
                ForEach(model.visible) { conversation in
                    Button {
                        onOpen(conversation.id)
                    } label: {
                        HistoryRow(
                            conversation: conversation,
                            snippet: model.snippet(for: conversation),
                            isCurrent: conversation.id == current
                        )
                    }
                    .buttonStyle(.plain)
                    .swipeActions(edge: .trailing) {
                        Button("Delete", role: .destructive) {
                            Task {
                                if await model.delete(conversation) {
                                    onDeleted(conversation.id)
                                }
                            }
                        }
                    }
                }
            } header: {
                Text(header)
            } footer: {
                if model.visible.isEmpty, model.hasLoaded {
                    Text(
                        model.isSearchActive
                            ? "Nothing matching that."
                            : "No conversations yet."
                    )
                }
            }
        }
        .searchable(
            text: $model.query,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Search titles and messages"
        )
        .task { await model.load() }
        .task(id: model.query) { await model.searchAfterTyping() }
        .refreshable { await model.load() }
        .overlay {
            if model.isLoading, !model.hasLoaded {
                ProgressView()
            }
        }
    }

    private var header: String {
        if model.isSearchActive {
            return model.isSearching ? "Searching…" : "Matches"
        }
        return "\(model.conversations.count) conversations"
    }
}

/// One thread: title, when, how long, and where a search hit it.
private struct HistoryRow: View {
    let conversation: Conversation
    let snippet: String?
    let isCurrent: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(conversation.displayTitle)
                    .font(.body.weight(isCurrent ? .semibold : .regular))
                    .lineLimit(2)
                Spacer()
                if isCurrent {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.tint)
                        .accessibilityLabel("Open now")
                }
            }
            HStack(spacing: 6) {
                if let when = conversation.lastMessageAt {
                    Text(when, format: .relative(presentation: .named))
                } else {
                    Text("Empty")
                }
                Text("·")
                Text("\(conversation.messageCount) messages")
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
            if let snippet {
                Text(snippet)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 2)
        .contentShape(Rectangle())
    }
}
