import SwiftUI

/// Connect a GitHub webhook so the robot reacts to your builds.
///
/// The secret appears exactly once, right after connecting, in a box with a
/// copy button. That is not a UI choice; the server does not return it
/// again. The screen says so before the button is pressed.
struct GitHubIntegrationView: View {
    @Environment(NovaAPI.self) private var api

    @State private var integration: GitHubIntegration?
    @State private var freshSecret: String?
    @State private var repository = ""
    @State private var isLoading = true
    @State private var isWorking = false
    @State private var error: String?
    @State private var confirmDisconnect = false
    @State private var confirmRotate = false

    var body: some View {
        Form {
            if isLoading {
                ProgressView()
            } else if let integration {
                connected(integration)
            } else {
                notConnected
            }

            if let error {
                Section { Text(error).foregroundStyle(.red) }
            }
        }
        .navigationTitle("GitHub")
        .task { await load() }
        .confirmationDialog(
            "Disconnect GitHub?", isPresented: $confirmDisconnect, titleVisibility: .visible
        ) {
            Button("Disconnect", role: .destructive) { Task { await disconnect() } }
        } message: {
            Text("The webhook URL stops working immediately. GitHub will keep trying it until you delete the hook there too.")
        }
        .confirmationDialog(
            "Rotate the secret?", isPresented: $confirmRotate, titleVisibility: .visible
        ) {
            Button("Rotate", role: .destructive) { Task { await connect() } }
        } message: {
            Text("The old secret stops verifying at once. You will need to paste the new one into the webhook on GitHub.")
        }
    }

    // MARK: - States

    private var notConnected: some View {
        Group {
            Section {
                TextField("owner/repository (optional)", text: $repository)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Repository")
            } footer: {
                Text("Leave empty to react to any repository the hook covers. Set it to ignore deliveries from anywhere else.")
            }

            Section {
                Button("Connect") { Task { await connect() } }
                    .disabled(isWorking)
            } footer: {
                Text(
                    "NOVA reacts on the desk when a workflow finishes or a pull request merges: "
                    + "a happy face for a green build, a confused one for red. "
                    + "You will get a webhook URL and a secret to paste into GitHub. "
                    + "The secret is shown once."
                )
            }
        }
    }

    @ViewBuilder
    private func connected(_ integration: GitHubIntegration) -> some View {
        if let freshSecret {
            Section {
                LabeledContent("Secret") {
                    Text(freshSecret)
                        .font(.system(.footnote, design: .monospaced))
                        .textSelection(.enabled)
                }
                Button("Copy secret") {
                    UIPasteboard.general.string = freshSecret
                }
            } header: {
                Text("Paste this into GitHub now")
            } footer: {
                Text("This is the only time it is shown. If it is lost, rotate it.")
            }
        }

        Section {
            LabeledContent("Webhook URL") {
                Text(integration.webhookUrl)
                    .font(.system(.footnote, design: .monospaced))
                    .textSelection(.enabled)
                    .multilineTextAlignment(.trailing)
            }
            Button("Copy URL") {
                UIPasteboard.general.string = integration.webhookUrl
            }
        } header: {
            Text("GitHub → Settings → Webhooks")
        } footer: {
            if integration.isAbsoluteURL {
                Text("Content type: application/json. Events: Workflow runs, Pull requests.")
            } else {
                Text(
                    "The server does not know its public address, so this is a path. "
                    + "Prefix it with where the API is reachable from the internet, "
                    + "or set NOVA_PUBLIC_BASE_URL on the server."
                )
            }
        }

        Section("Status") {
            LabeledContent("Repository", value: integration.repository ?? "Any")
            Toggle("Enabled", isOn: Binding(
                get: { integration.enabled },
                set: { newValue in Task { await update(enabled: newValue) } }
            ))
            .disabled(isWorking)
            LabeledContent("Last delivery") {
                if let at = integration.lastDeliveryAt {
                    Text(at, style: .relative) + Text(" ago")
                } else {
                    Text("Never")
                }
            }
            if let event = integration.lastEvent {
                LabeledContent("Last event", value: event)
            }
        }

        Section {
            Button("Rotate secret") { confirmRotate = true }
            Button("Disconnect", role: .destructive) { confirmDisconnect = true }
        }
        .disabled(isWorking)
    }

    // MARK: - Actions

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            integration = try await api.githubIntegration()
        } catch APIError.api(_, let envelope) where envelope.code == "github_integration_not_found" {
            // Not connected yet. Branch on the stable code, never the message.
            integration = nil
        } catch {
            self.error = describe(error)
        }
    }

    private func connect() async {
        await work {
            let trimmed = repository.trimmingCharacters(in: .whitespaces)
            let created = try await api.connectGitHub(repository: trimmed.isEmpty ? nil : trimmed)
            integration = created.integration
            freshSecret = created.secret
        }
    }

    private func update(enabled: Bool) async {
        await work {
            integration = try await api.updateGitHubIntegration(repository: nil, enabled: enabled)
        }
    }

    private func disconnect() async {
        await work {
            try await api.disconnectGitHub()
            integration = nil
            freshSecret = nil
        }
    }

    private func work(_ action: () async throws -> Void) async {
        isWorking = true
        error = nil
        defer { isWorking = false }
        do { try await action() } catch { self.error = describe(error) }
    }

    private func describe(_ error: Error) -> String {
        (error as? APIError)?.userMessage ?? error.localizedDescription
    }
}
