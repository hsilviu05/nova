import SwiftUI

struct SettingsView: View {
    @Environment(SessionStore.self) private var session
    @Environment(ServerSettings.self) private var server
    @Environment(VoiceStore.self) private var voice
    let user: User

    @State private var displayName = ""
    @State private var isSigningOut = false

    var body: some View {
        @Bindable var voice = voice

        NavigationStack {
            List {
                Section {
                    NavigationLink {
                        ServerView()
                    } label: {
                        LabeledContent("NOVA server") {
                            Text(server.baseURL.absoluteString)
                                .font(.callout.monospaced())
                                .lineLimit(1)
                                .truncationMode(.head)
                        }
                    }
                } header: {
                    Text("Connection")
                } footer: {
                    if server.isLoopback {
                        // The single most likely first-run mistake, said
                        // once and plainly: on the phone, 127.0.0.1 is the
                        // phone, not the Mac NOVA is running on.
                        Text(
                            "This is the phone's own address. To reach NOVA on your Mac, "
                            + "use the Mac's address on your network, like "
                            + "http://192.168.1.20:8000"
                        )
                    }
                }

                Section("Account") {
                    LabeledContent("Email", value: user.email)

                    HStack {
                        Text("Name")
                        Spacer()
                        TextField("Name", text: $displayName)
                            .multilineTextAlignment(.trailing)
                            .textContentType(.name)
                            .submitLabel(.done)
                            .onSubmit {
                                Task { await session.updateDisplayName(displayName) }
                            }
                    }

                    NavigationLink {
                        TimezonePicker(current: user.timezone)
                    } label: {
                        LabeledContent("Time zone", value: user.timezone)
                    }
                }

                Section {
                    Toggle("Read replies aloud", isOn: $voice.speaksReplies)
                } header: {
                    Text("Voice")
                } footer: {
                    Text(
                        "Hold the microphone in a conversation to talk. What you said "
                        + "goes into the box to check before it is sent — nothing is "
                        + "sent by voice without being seen."
                    )
                }

                Section {
                    NavigationLink {
                        GitHubIntegrationView()
                    } label: {
                        Label("GitHub", systemImage: "chevron.left.forwardslash.chevron.right")
                    }
                } footer: {
                    Text("Let NOVA react on the desk when your builds finish.")
                }

                Section {
                    Button("Sign Out", role: .destructive) { isSigningOut = true }
                }

                Section {
                    LabeledContent("Version", value: Bundle.main.shortVersion)
                } footer: {
                    Text(
                        "NOVA is a local-first personal AI terminal. The model and your "
                        + "memories stay on your own machine."
                    )
                }
            }
            .navigationTitle("Settings")
            .onAppear { displayName = user.displayName }
            .onChange(of: user.displayName) { _, name in displayName = name }
            .confirmationDialog(
                "Sign out?", isPresented: $isSigningOut, titleVisibility: .visible
            ) {
                Button("Sign Out", role: .destructive) {
                    Task { await session.signOut() }
                }
            } message: {
                Text("NOVA keeps running. You'll need to sign in again to reach it.")
            }
        }
    }
}

/// Where NOVA is.
///
/// A whole screen for one field, because it is the field that decides whether
/// the app works at all, and because getting it wrong should be explained
/// rather than answered with a stream of connection errors.
private struct ServerView: View {
    @Environment(ServerSettings.self) private var server
    @Environment(\.dismiss) private var dismiss

    @State private var address = ""
    @State private var problem: String?

    var body: some View {
        Form {
            Section {
                TextField("http://192.168.1.20:8000", text: $address)
                    .font(.body.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .submitLabel(.done)
                    .onSubmit(save)
            } header: {
                Text("Address")
            } footer: {
                if let problem {
                    Text(problem).foregroundStyle(.red)
                } else {
                    Text(
                        "The address of the Mac or server running NOVA, on your network. "
                        + "http:// works for an address on your own network; anything "
                        + "else needs https://."
                    )
                }
            }

            Section {
                Button("Save", action: save)
                    .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                Button("Reset to default", role: .destructive) {
                    server.reset()
                    address = server.baseURL.absoluteString
                    problem = nil
                }
            } footer: {
                Text(
                    "Changing this signs you out, because an account on one NOVA is not "
                    + "an account on another."
                )
            }
        }
        .navigationTitle("NOVA server")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { address = server.baseURL.absoluteString }
    }

    private func save() {
        do {
            try server.update(to: address)
            problem = nil
            dismiss()
        } catch {
            problem = error.message
        }
    }
}

extension Bundle {
    var shortVersion: String {
        let version = object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let build = object(forInfoDictionaryKey: "CFBundleVersion") as? String
        return "\(version ?? "0.1.0") (\(build ?? "1"))"
    }
}
