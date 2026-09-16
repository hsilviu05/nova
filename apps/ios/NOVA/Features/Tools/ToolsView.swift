import SwiftUI

/// Everything NOVA can do, and a way to do it directly.
///
/// This screen exists for two reasons beyond convenience. It is where someone
/// can see the full list of capabilities their NOVA has -- which matters when
/// the alternative is inferring them from what the model happens to mention.
/// And it is where a destructive action gets its confirmation, which is the
/// same sheet the chat screen raises, so the moment of consent looks
/// identical wherever it is reached from.
struct ToolsView: View {
    @Environment(\.novaAPI) private var api
    @State private var model = ToolsModel()

    var body: some View {
        NavigationStack {
            List {
                if model.shellEnabled {
                    Section {
                        Label(
                            "Shell execution is enabled on this server.",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .foregroundStyle(.orange)
                        .font(.subheadline)
                    }
                }

                ForEach(model.groups, id: \.self) { group in
                    Section(group.capitalized) {
                        ForEach(model.tools(in: group)) { tool in
                            NavigationLink {
                                ToolDetailView(tool: tool, model: model)
                            } label: {
                                ToolRow(tool: tool)
                            }
                        }
                    }
                }

                if let error = model.error {
                    Section { InlineError(error: error) }
                }
            }
            .navigationTitle("Tools")
            .overlay {
                if model.tools.isEmpty, model.isLoading {
                    ProgressView()
                } else if model.tools.isEmpty, !model.isLoading {
                    ContentUnavailableView(
                        "No tools",
                        systemImage: "wrench.and.screwdriver",
                        description: Text(
                            "This NOVA has no capabilities enabled. Turn a group on in the server's configuration."
                        )
                    )
                }
            }
            .refreshable { await model.load(using: api) }
        }
        .task { await model.load(using: api) }
        .sheet(item: $model.pending) { confirmation in
            ConfirmationSheet(
                confirmation: confirmation,
                onConfirm: { Task { await model.confirm(confirmation, using: api) } },
                onCancel: { model.decline() }
            )
        }
    }
}

private struct ToolRow: View {
    let tool: NovaTool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(tool.name)
                    .font(.body.monospaced())
                if tool.permission.changesThings {
                    PermissionBadge(permission: tool.permission)
                }
            }
            Text(tool.description)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .padding(.vertical, 2)
    }
}

/// Says plainly that a tool changes something, before anyone taps it.
struct PermissionBadge: View {
    let permission: NovaTool.Permission

    private var label: String {
        switch permission {
        case .read: "reads"
        case .write: "writes"
        case .destructive: "changes things"
        case .unknown: "unknown"
        }
    }

    private var tint: Color {
        permission == .write ? .blue : .orange
    }

    var body: some View {
        Text(label)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(tint.opacity(0.16), in: .capsule)
            .foregroundStyle(tint)
    }
}

/// One tool, its arguments, and a button.
private struct ToolDetailView: View {
    let tool: NovaTool
    @Bindable var model: ToolsModel
    @Environment(\.novaAPI) private var api

    @State private var arguments: [String: String] = [:]

    var body: some View {
        Form {
            Section {
                Text(tool.description)
                    .font(.subheadline)
            } header: {
                HStack {
                    Text(tool.group.capitalized)
                    Spacer()
                    PermissionBadge(permission: tool.permission)
                }
            }

            if !tool.argumentNames.isEmpty {
                Section("Arguments") {
                    ForEach(tool.argumentNames, id: \.self) { name in
                        LabeledContent {
                            TextField(
                                tool.isRequired(name) ? "required" : "optional",
                                text: binding(for: name)
                            )
                            .multilineTextAlignment(.trailing)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                        } label: {
                            Text(name).font(.callout.monospaced())
                        }
                    }
                }
            }

            Section {
                Button {
                    Task { await model.run(tool, arguments: filled, using: api) }
                } label: {
                    if model.running == tool.name {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Running…")
                        }
                    } else {
                        Text(tool.permission.changesThings ? "Run…" : "Run")
                    }
                }
                .disabled(model.running != nil)
            } footer: {
                if tool.requiresConfirmation {
                    Text("You'll be asked to confirm before anything happens.")
                }
            }

            if let result = model.lastResult, result.tool == tool.name {
                Section(result.isError ? "Failed" : "Result") {
                    Text(result.content)
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)
                    if result.truncated {
                        Text("Output was truncated.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    LabeledContent("Took", value: "\(result.durationMs) ms")
                }
            }

            if let error = model.error {
                Section { InlineError(error: error) }
            }
        }
        .navigationTitle(tool.name)
        .navigationBarTitleDisplayMode(.inline)
    }

    /// Only the arguments that were actually filled in.
    ///
    /// Sending an empty string for an untouched optional would be a value,
    /// not an omission, and the server's schemas reject blanks rather than
    /// guessing which was meant.
    private var filled: [String: String] {
        arguments.filter { !$0.value.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    private func binding(for name: String) -> Binding<String> {
        Binding(
            get: { arguments[name] ?? "" },
            set: { arguments[name] = $0 }
        )
    }
}

/// The moment of consent.
///
/// Raised identically from the Tools screen and from a conversation, because
/// the decision is the same one and it should not look different depending on
/// how it was reached. The prompt is the server's words, not the app's: only
/// the server knows what the call would actually do.
struct ConfirmationSheet: View {
    let confirmation: PendingConfirmation
    let onConfirm: () -> Void
    let onCancel: () -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 44))
                    .foregroundStyle(.orange)
                    .padding(.top, 32)

                Text(confirmation.prompt)
                    .font(.title3.weight(.medium))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)

                if !confirmation.arguments.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(confirmation.arguments.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                            HStack {
                                Text(key).foregroundStyle(.secondary)
                                Spacer()
                                Text(value)
                            }
                            .font(.footnote.monospaced())
                        }
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity)
                    .card()
                    .padding(.horizontal, 24)
                }

                Spacer(minLength: 0)

                VStack(spacing: 10) {
                    Button(role: .destructive) {
                        onConfirm()
                        dismiss()
                    } label: {
                        Text("Yes, do it")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)

                    Button("Not now") {
                        onCancel()
                        dismiss()
                    }
                    .controlSize(.large)
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 24)
            }
            .navigationTitle(confirmation.tool)
            .navigationBarTitleDisplayMode(.inline)
            .interactiveDismissDisabled()
        }
        .presentationDetents([.medium])
    }
}
