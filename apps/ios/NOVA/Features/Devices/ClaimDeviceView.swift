import SwiftUI

/// Adopt the NOVA showing a code on its face.
///
/// Manual entry. The server accepts the code in any reasonable form —
/// lower case, no hyphen, stray spaces — so this screen normalises for
/// legibility rather than validation, and never blocks a submission over
/// formatting.
struct ClaimDeviceView: View {
    @Environment(\.novaAPI) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var code = ""
    @State private var name = ""
    @State private var isWorking = false
    @State private var error: APIError?
    @State private var claimed: Device?
    @FocusState private var codeFocused: Bool

    /// Six characters, hyphen excluded — it is inserted for display only.
    private static let codeLength = 6

    private var normalised: String {
        code
            .uppercased()
            .filter { $0.isLetter || $0.isNumber }
            .prefix(Self.codeLength)
            .description
    }

    private var canSubmit: Bool {
        normalised.count == Self.codeLength && !isWorking
    }

    var body: some View {
        NavigationStack {
            Group {
                if let claimed {
                    SuccessView(device: claimed) { dismiss() }
                } else {
                    form
                }
            }
            .navigationTitle(claimed == nil ? "Add NOVA" : "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if claimed == nil {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { dismiss() }
                    }
                }
            }
        }
    }

    private var form: some View {
        ScrollView {
            VStack(spacing: 28) {
                instructions
                codeField
                nameField

                if let error {
                    InlineError(error: error)
                }

                Button {
                    Task { await claim() }
                } label: {
                    Group {
                        if isWorking {
                            ProgressView().tint(.white)
                        } else {
                            Text("Claim").fontWeight(.semibold)
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: 26)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(!canSubmit)
            }
            .padding(24)
        }
        .background(Color(.systemGroupedBackground))
        .scrollDismissesKeyboard(.interactively)
        .onAppear { codeFocused = true }
        .animation(.easeInOut(duration: 0.2), value: error != nil)
    }

    private var instructions: some View {
        VStack(spacing: 12) {
            NovaMark(size: 64)
            Text("Type the code on NOVA's face")
                .font(.title3.weight(.semibold))
                .multilineTextAlignment(.center)
            Text("It changes every ten minutes. If it has expired, restart NOVA for a fresh one.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }

    private var codeField: some View {
        VStack(spacing: 10) {
            TextField("ABC-123", text: $code)
                .font(.system(size: 34, weight: .semibold, design: .monospaced))
                .multilineTextAlignment(.center)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
                .textContentType(.oneTimeCode)
                .focused($codeFocused)
                .submitLabel(.go)
                .onSubmit { Task { await claim() } }
                .onChange(of: code) { _, _ in
                    // Reformat as they type, and clear a stale failure so the
                    // screen does not keep accusing them of an old mistake.
                    code = formatted(normalised)
                    if error != nil { error = nil }
                }
                .padding(.vertical, 18)
                .frame(maxWidth: .infinity)
                .background(
                    Color(.secondarySystemGroupedBackground),
                    in: .rect(cornerRadius: 16)
                )

            Text("\(normalised.count) of \(Self.codeLength)")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .monospacedDigit()
        }
    }

    private var nameField: some View {
        VStack(alignment: .leading, spacing: 6) {
            LabeledField("Name (optional)", text: $name)
            Text("Defaults to the model name.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// Insert the hyphen for readability. The server strips it anyway.
    private func formatted(_ raw: String) -> String {
        guard raw.count > 3 else { return raw }
        let index = raw.index(raw.startIndex, offsetBy: 3)
        return "\(raw[..<index])-\(raw[index...])"
    }

    private func claim() async {
        guard canSubmit else { return }
        codeFocused = false
        isWorking = true
        error = nil
        defer { isWorking = false }

        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)

        do {
            claimed = try await api.claimDevice(
                code: normalised,
                name: trimmedName.isEmpty ? nil : trimmedName
            )
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }
}

/// Shown once the device is bound to the account.
private struct SuccessView: View {
    let device: Device
    let done: () -> Void

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            NovaMark(size: 96)
            VStack(spacing: 8) {
                Text("\(device.displayName) is yours")
                    .font(.title2.weight(.semibold))
                Text("It will come online once it finishes connecting.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Spacer()
            Button("Done", action: done)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .frame(maxWidth: .infinity)
        }
        .padding(24)
        .background(Color(.systemGroupedBackground))
    }
}
