import SwiftUI

/// Sign in, or create an account. One screen, because the difference between
/// them is one field.
struct SignInView: View {
    @Environment(SessionStore.self) private var session

    @State private var mode: Mode = .signIn
    @State private var email = ""
    @State private var password = ""
    @State private var displayName = ""
    @FocusState private var focused: Field?

    private enum Mode {
        case signIn, register

        var title: String {
            switch self {
            case .signIn: "Welcome back"
            case .register: "Hello"
            }
        }

        var action: String {
            switch self {
            case .signIn: "Sign In"
            case .register: "Create Account"
            }
        }
    }

    private enum Field {
        case name, email, password
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 28) {
                header
                fields
                errorBanner
                actions
            }
            .padding(.horizontal, 24)
            .padding(.top, 56)
            .padding(.bottom, 32)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Color(.systemGroupedBackground))
        .animation(.easeInOut(duration: 0.2), value: mode)
        .animation(.easeInOut(duration: 0.2), value: session.error != nil)
    }

    private var header: some View {
        VStack(spacing: 16) {
            NovaMark(size: 84)
            Text(mode.title)
                .font(.largeTitle.weight(.semibold))
            Text("NOVA lives on your desk and remembers.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }

    private var fields: some View {
        VStack(spacing: 12) {
            if mode == .register {
                LabeledField(
                    "Name",
                    text: $displayName,
                    error: fieldError("display_name")
                )
                .textContentType(.name)
                .focused($focused, equals: .name)
                .submitLabel(.next)
                .onSubmit { focused = .email }
            }

            LabeledField("Email", text: $email, error: fieldError("email"))
                .textContentType(.emailAddress)
                .keyboardType(.emailAddress)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .focused($focused, equals: .email)
                .submitLabel(.next)
                .onSubmit { focused = .password }

            LabeledField(
                "Password",
                text: $password,
                error: fieldError("password"),
                isSecure: true
            )
            .textContentType(mode == .register ? .newPassword : .password)
            .focused($focused, equals: .password)
            .submitLabel(.go)
            .onSubmit { Task { await submit() } }

            if mode == .register {
                Text("At least 12 characters.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    @ViewBuilder
    private var errorBanner: some View {
        // Field-level problems are shown inline on the field itself; this is
        // for everything else.
        if let error = session.error, error.fieldErrors.isEmpty {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text(error.userMessage)
                        .font(.subheadline)
                    if let requestId = error.requestId {
                        // Lets a user quote the exact request in a bug report.
                        Text(requestId)
                            .font(.caption2.monospaced())
                            .foregroundStyle(.tertiary)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(12)
            .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 12))
            .transition(.opacity.combined(with: .move(edge: .top)))
        }
    }

    private var actions: some View {
        VStack(spacing: 16) {
            Button {
                Task { await submit() }
            } label: {
                Group {
                    if session.isWorking {
                        ProgressView().tint(.white)
                    } else {
                        Text(mode.action).fontWeight(.semibold)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 26)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!canSubmit)

            Button {
                session.error = nil
                mode = mode == .signIn ? .register : .signIn
            } label: {
                Text(
                    mode == .signIn
                        ? "No account yet? Create one"
                        : "Already have an account? Sign in"
                )
                .font(.subheadline)
            }
        }
    }

    private var canSubmit: Bool {
        guard !session.isWorking,
              email.contains("@"),
              password.count >= (mode == .register ? 12 : 1)
        else { return false }

        if mode == .register {
            return !displayName.trimmingCharacters(in: .whitespaces).isEmpty
        }
        return true
    }

    private func fieldError(_ name: String) -> String? {
        session.error?.fieldErrors[name]
    }

    private func submit() async {
        guard canSubmit else { return }
        focused = nil

        switch mode {
        case .signIn:
            await session.signIn(email: email, password: password)
        case .register:
            await session.register(
                email: email, password: password, displayName: displayName
            )
        }
    }
}

/// A labelled text field that can show a validation message underneath.
struct LabeledField: View {
    let title: String
    @Binding var text: String
    var error: String?
    var isSecure: Bool = false

    init(
        _ title: String,
        text: Binding<String>,
        error: String? = nil,
        isSecure: Bool = false
    ) {
        self.title = title
        self._text = text
        self.error = error
        self.isSecure = isSecure
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Group {
                if isSecure {
                    SecureField(title, text: $text)
                } else {
                    TextField(title, text: $text)
                }
            }
            .textFieldStyle(.plain)
            .padding(.horizontal, 14)
            .padding(.vertical, 13)
            .background(
                Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 12)
            )
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(error == nil ? .clear : Color.red.opacity(0.6))
            }

            if let error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: error)
    }
}
