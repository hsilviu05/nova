import SwiftUI

/// Talking to NOVA.
struct ChatView: View {
    @Environment(\.novaAPI) private var api
    @Environment(\.chatStream) private var stream

    @State private var model: ChatModel?
    @State private var draft = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            Group {
                if let model {
                    transcript(model)
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .navigationTitle("NOVA")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await model?.startNewConversation() }
                    } label: {
                        Label("New conversation", systemImage: "square.and.pencil")
                    }
                    .disabled(model?.canSend == false)
                }
            }
        }
        .task {
            if model == nil {
                model = ChatModel(api: api, stream: stream)
            }
            await model?.load()
        }
    }

    private func transcript(_ model: ChatModel) -> some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if model.messages.isEmpty {
                            EmptyTranscript()
                        }

                        ForEach(model.messages) { message in
                            MessageBubble(message: message)
                                .id(message.id)
                        }

                        if model.activity == .thinking {
                            ThinkingIndicator().id(thinkingAnchor)
                        }

                        if let error = model.error {
                            InlineError(error: error)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 20)
                }
                .scrollDismissesKeyboard(.interactively)
                // Follow the newest content, including each delta, so a long
                // reply stays in view as it is written.
                .onChange(of: model.messages.last?.content) { _, _ in
                    scrollToEnd(proxy, model)
                }
                .onChange(of: model.activity) { _, _ in
                    scrollToEnd(proxy, model)
                }
            }

            Composer(
                draft: $draft,
                isFocused: $inputFocused,
                activity: model.activity,
                onSend: {
                    model.send(draft)
                    draft = ""
                },
                onStop: { model.cancel() }
            )
        }
        .background(Color(.systemGroupedBackground))
    }

    private var thinkingAnchor: String { "thinking" }

    private func scrollToEnd(_ proxy: ScrollViewProxy, _ model: ChatModel) {
        let target: AnyHashable? =
            model.activity == .thinking ? thinkingAnchor : model.messages.last?.id

        guard let target else { return }
        withAnimation(.easeOut(duration: 0.2)) {
            proxy.scrollTo(target, anchor: .bottom)
        }
    }
}

private struct EmptyTranscript: View {
    var body: some View {
        VStack(spacing: 14) {
            NovaMark(size: 64)
            Text("Say something.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }
}

private struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.isFromNova {
                content
                Spacer(minLength: 40)
            } else {
                Spacer(minLength: 40)
                content
            }
        }
    }

    private var content: some View {
        Text(message.content)
            .font(.body)
            .textSelection(.enabled)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(background, in: .rect(cornerRadius: 18))
            .foregroundStyle(message.isFromNova ? Color.primary : Color.white)
            .frame(
                maxWidth: .infinity,
                alignment: message.isFromNova ? .leading : .trailing
            )
    }

    private var background: Color {
        message.isFromNova ? Color(.secondarySystemGroupedBackground) : .accentColor
    }
}

/// Three dots, shown only while NOVA is thinking.
///
/// Replaced by the reply itself as soon as the first delta arrives, so it
/// never appears alongside text.
private struct ThinkingIndicator: View {
    @State private var phase = 0.0

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(Color.secondary)
                    .frame(width: 7, height: 7)
                    .opacity(opacity(for: index))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 18))
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear {
            withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) {
                phase = 3
            }
        }
        .accessibilityLabel("NOVA is thinking")
    }

    private func opacity(for index: Int) -> Double {
        let distance = abs(phase - Double(index))
        return max(0.25, 1 - distance / 1.5)
    }
}

private struct Composer: View {
    @Binding var draft: String
    @FocusState.Binding var isFocused: Bool
    let activity: ChatModel.Activity
    let onSend: () -> Void
    let onStop: () -> Void

    private var canSend: Bool {
        activity == .idle && !draft.trimmingCharacters(in: .whitespaces).isEmpty
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Message", text: $draft, axis: .vertical)
                .lineLimit(1...5)
                .focused($isFocused)
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .background(
                    Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 20)
                )
                .onSubmit(onSend)

            Button(action: activity == .idle ? onSend : onStop) {
                Image(systemName: activity == .idle ? "arrow.up" : "stop.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 34, height: 34)
                    .background(
                        (canSend || activity != .idle) ? Color.accentColor : Color.secondary,
                        in: .circle
                    )
            }
            .disabled(activity == .idle && !canSend)
            .accessibilityLabel(activity == .idle ? "Send" : "Stop")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.bar)
    }
}
