import SwiftUI

struct SettingsView: View {
    @Environment(SessionStore.self) private var session
    let user: User

    @State private var displayName = ""
    @State private var isSigningOut = false

    var body: some View {
        NavigationStack {
            List {
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

                    if let joined = user.createdAt as Date? {
                        LabeledContent("Member since") {
                            Text(joined, format: .dateTime.month(.abbreviated).year())
                        }
                    }
                }

                Section {
                    Button("Sign Out", role: .destructive) { isSigningOut = true }
                }

                Section {
                    LabeledContent("Version", value: Bundle.main.shortVersion)
                } footer: {
                    Text("NOVA is a physical AI companion. This is Phase 3 — chat, memory and insights arrive in later releases.")
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
                Text("Your NOVA keeps running. You'll need to sign in again to see it.")
            }
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
