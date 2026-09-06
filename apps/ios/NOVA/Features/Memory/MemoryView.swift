import SwiftUI

/// What NOVA remembers about you, and how to change it.
///
/// This screen is part of the feature rather than an admin surface. NOVA
/// forms beliefs about the person it sits with from things they said in
/// passing; being able to read, correct, and delete those beliefs is what
/// makes that acceptable instead of unnerving.
struct MemoryView: View {
    @Environment(\.novaAPI) private var api
    @State private var model: MemoryModel?

    var body: some View {
        NavigationStack {
            if let model {
                MemoryList(model: model)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task {
            // Built here rather than in an initialiser: the model needs the
            // API from the environment, which is not available until the
            // view is in a hierarchy.
            if model == nil { model = MemoryModel(api: api) }
        }
    }
}

/// The list itself, separated so the model can be `@Bindable`.
private struct MemoryList: View {
    @Bindable var model: MemoryModel

    @State private var editing: Memory?
    @State private var isConfirmingForget = false

    var body: some View {
        content
            .navigationTitle("Memory")
            .toolbar {
                if !model.memories.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Forget all", role: .destructive) {
                            isConfirmingForget = true
                        }
                    }
                }
            }
            // Reloads on first appearance and whenever the filter changes;
            // the previous load is cancelled rather than racing this one.
            .task(id: model.filter) { await model.load() }
            .task(id: model.query) { await model.searchAfterTyping() }
            .confirmationDialog(
                "Forget everything?",
                isPresented: $isConfirmingForget,
                titleVisibility: .visible
            ) {
                Button("Forget everything", role: .destructive) {
                    Task { await model.forgetEverything() }
                }
            } message: {
                Text(
                    "NOVA keeps your device and your conversations but stops knowing anything about you."
                )
            }
            .sheet(item: $editing) { memory in
                EditMemorySheet(memory: memory, model: model)
            }
    }

    @ViewBuilder
    private var content: some View {
        if model.isFirstLoad {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if model.isEmpty {
            EmptyMemoryView()
        } else {
            list
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                Section { InlineError(error: error) }
            }

            if !model.isSearchActive {
                Section {
                    CategoryFilter(selection: $model.filter)
                }
                .listRowInsets(EdgeInsets())
                .listRowBackground(Color.clear)
            }

            Section {
                ForEach(model.visible) { memory in
                    MemoryRow(memory: memory, similarity: model.similarity(for: memory))
                        .contentShape(.rect)
                        .onTapGesture { editing = memory }
                        .swipeActions(edge: .trailing) {
                            Button("Forget", role: .destructive) {
                                Task { await model.delete(memory) }
                            }
                        }
                }
            } header: {
                Text(header)
            } footer: {
                if model.visible.isEmpty {
                    Text(
                        model.isSearchActive
                            ? "Nothing matching that."
                            : "Nothing in this category yet."
                    )
                }
            }
        }
        .searchable(
            text: $model.query,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Search what NOVA knows"
        )
        .refreshable { await model.load() }
    }

    private var header: String {
        if model.isSearchActive {
            return model.isSearching ? "Searching…" : "Closest matches"
        }
        return model.filter == nil ? "\(model.total) remembered" : "Filtered"
    }
}

/// One memory, with enough context to judge it.
private struct MemoryRow: View {
    let memory: Memory
    let similarity: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(memory.content)
                .font(.callout)

            HStack(spacing: 8) {
                Label(memory.category.label, systemImage: memory.category.symbol)
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text("·").foregroundStyle(.tertiary)

                Text(memory.recallDescription)
                    .font(.caption)
                    .foregroundStyle(.tertiary)

                Spacer(minLength: 0)

                if let similarity {
                    // Shown during a search so it is clear why a result is
                    // here. Retrieval you cannot see is retrieval you cannot
                    // judge.
                    Text(similarity.formatted(.percent.precision(.fractionLength(0))))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

private struct CategoryFilter: View {
    @Binding var selection: MemoryCategory?

    var body: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                chip(nil, label: "All", symbol: "square.grid.2x2")
                ForEach(MemoryCategory.selectable) { category in
                    chip(category, label: category.label, symbol: category.symbol)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 8)
        }
        .scrollIndicators(.hidden)
    }

    private func chip(
        _ category: MemoryCategory?, label: String, symbol: String
    ) -> some View {
        let isSelected = selection == category

        return Button {
            selection = category
        } label: {
            Label(label, systemImage: symbol)
                .font(.caption.weight(.medium))
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(
                    isSelected ? Color.accentColor.opacity(0.18) : Color(.tertiarySystemFill),
                    in: .capsule
                )
                .foregroundStyle(isSelected ? Color.accentColor : Color.primary)
        }
        .buttonStyle(.plain)
    }
}

/// Correcting something NOVA believes.
private struct EditMemorySheet: View {
    let memory: Memory
    let model: MemoryModel

    @Environment(\.dismiss) private var dismiss
    @State private var content: String
    @State private var category: MemoryCategory
    @State private var isSaving = false

    init(memory: Memory, model: MemoryModel) {
        self.memory = memory
        self.model = model
        _content = State(initialValue: memory.content)
        // A category this build does not recognise has to become something
        // the picker can show, or there is no selection at all.
        _category = State(
            initialValue: memory.category == .unknown ? .fact : memory.category
        )
    }

    private var canSave: Bool {
        !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isSaving
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("What NOVA remembers") {
                    TextField("Memory", text: $content, axis: .vertical)
                        .lineLimit(2...6)
                }

                Section {
                    Picker("Category", selection: $category) {
                        ForEach(MemoryCategory.selectable) { option in
                            Label(option.label, systemImage: option.symbol)
                                .tag(option)
                        }
                    }
                } footer: {
                    Text("Editing the wording changes what brings this to mind later.")
                }

                Section {
                    Button("Forget this", role: .destructive) {
                        Task {
                            await model.delete(memory)
                            dismiss()
                        }
                    }
                } footer: {
                    if let recalled = memory.lastRecalledAt {
                        Text("Last used \(recalled, format: .relative(presentation: .named)).")
                    }
                }
            }
            .navigationTitle("Correct memory")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        isSaving = true
                        Task {
                            await model.update(memory, content: content, category: category)
                            dismiss()
                        }
                    }
                    .disabled(!canSave)
                }
            }
        }
    }
}

private struct EmptyMemoryView: View {
    var body: some View {
        VStack(spacing: 16) {
            NovaMark(size: 64, isAwake: false)
            Text("Nothing remembered yet")
                .font(.title3.weight(.semibold))
            Text(
                "NOVA picks things up from what you tell it. Talk to it for a while and they will show up here."
            )
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemGroupedBackground))
    }
}
