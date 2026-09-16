import SwiftUI

/// Where NOVA is.
///
/// A whole screen for one field, because it is the field that decides whether
/// the app works at all, and because getting it wrong should be explained
/// rather than answered with a stream of connection errors.
///
/// Reachable from Settings *and* from the sign-in screen. It has to be: a
/// fresh install points at `127.0.0.1`, which on a phone is the phone, so
/// the first sign-in always fails until this is changed -- and Settings is
/// behind the sign-in that cannot succeed yet.
struct ServerView: View {
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
