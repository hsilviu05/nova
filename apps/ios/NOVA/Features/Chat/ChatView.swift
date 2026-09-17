import SwiftUI

/// The main way to use NOVA.
///
/// Sized for a phone on a stand: the text is a step larger than a chat app's
/// default, the microphone and send targets are comfortably past the 44pt
/// minimum, and the composer stays pinned so nothing has to be scrolled to
/// before it can be typed into.
struct ChatView: View {
    @Environment(\.novaAPI) private var api
    @Environment(\.chatStream) private var stream
    @Environment(VoiceStore.self) private var voice

    @State private var model: ChatModel?
    @State private var draft = ""
    @State private var isShowingHistory = false
    @FocusState private var isComposing: Bool

    var body: some View {
        NavigationStack {
            Group {
                if let model {
                    conversation(model)
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .navigationTitle("NOVA")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("History", systemImage: "clock.arrow.circlepath") {
                        isShowingHistory = true
                    }
                    .disabled(model == nil)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("New", systemImage: "square.and.pencil") {
                        Task { await model?.startNewConversation() }
                    }
                    .disabled(model?.isStreaming ?? true)
                }
            }
            .sheet(isPresented: $isShowingHistory) {
                if let model {
                    HistoryView(
                        api: api,
                        current: model.conversationID,
                        onOpen: { id in
                            isShowingHistory = false
                            Task { await model.open(id) }
                        },
                        onDeleted: { id in
                            Task { await model.conversationWasDeleted(id) }
                        }
                    )
                }
            }
        }
        .task {
            if model == nil {
                let created = ChatModel(api: api, stream: stream)
                model = created
                await created.load()
            }
        }
    }

    @ViewBuilder
    private func conversation(_ model: ChatModel) -> some View {
        @Bindable var model = model

        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(model.transcript) { item in
                            switch item {
                            case let .message(message):
                                MessageBubble(message: message)
                                    .id(item.id)
                            case let .tool(activity):
                                ToolChip(activity: activity)
                                    .id(item.id)
                            }
                        }

                        if model.activity == .thinking {
                            ThinkingIndicator().id("activity")
                        }

                        if let error = model.error {
                            FailureRow(error: error, canRetry: model.canRetry) {
                                model.retry()
                            }
                            .id("error")
                        }
                    }
                    .padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: model.transcript.count) { _, _ in
                    scrollToEnd(proxy, model)
                }
                .onChange(of: model.transcript.last) { _, _ in
                    scrollToEnd(proxy, model)
                }
            }

            Composer(
                draft: $draft,
                isComposing: $isComposing,
                model: model,
                onSend: { send(model) }
            )
        }
        .background(Color(.systemGroupedBackground))
        .sheet(item: $model.pendingConfirmation) { confirmation in
            ConfirmationSheet(
                confirmation: confirmation,
                onConfirm: { Task { await model.confirm() } },
                onCancel: { model.declineConfirmation() }
            )
        }
        .onChange(of: model.activity) { previous, current in
            // Read the finished reply aloud, but only when the person turned
            // speaking on and only once the reply is complete. Speaking
            // fragments as they stream sounds like a stutter.
            guard voice.speaksReplies, previous == .speaking, current == .idle,
                  let text = model.lastAssistantText
            else { return }
            voice.speak(text)
        }
    }

    private func send(_ model: ChatModel) {
        let text = draft
        draft = ""
        model.send(text)
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy, _ model: ChatModel) {
        let target = model.error != nil
            ? "error"
            : (model.activity == .thinking ? "activity" : model.transcript.last?.id)
        guard let target else { return }
        withAnimation(.easeOut(duration: 0.2)) {
            proxy.scrollTo(target, anchor: .bottom)
        }
    }
}

// MARK: - Transcript

private struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if !message.isFromNova { Spacer(minLength: 48) }

            VStack(alignment: message.isFromNova ? .leading : .trailing, spacing: 4) {
                // Markdown so code spans, fences and lists render as written.
                // `.full` because NOVA is told it may use Markdown when it
                // helps, and a terminal's answers are full of code.
                Text(attributed)
                    .font(.body)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(
                        message.isFromNova
                            ? AnyShapeStyle(Color(.secondarySystemGroupedBackground))
                            : AnyShapeStyle(Color.accentColor.opacity(0.18)),
                        in: .rect(cornerRadius: 16)
                    )
            }

            if message.isFromNova { Spacer(minLength: 48) }
        }
    }

    /// Parsed once per render rather than stored, because the content of a
    /// streaming bubble changes on every delta.
    ///
    /// Falls back to the raw text when parsing fails, which it does routinely
    /// mid-stream: half a fenced block is not valid Markdown, and the
    /// alternative to a fallback is text that flickers away and back.
    private var attributed: AttributedString {
        (try? AttributedString(
            markdown: message.content,
            options: .init(interpretedSyntax: .full, failurePolicy: .returnPartiallyParsedIfPossible)
        )) ?? AttributedString(message.content)
    }
}

/// What NOVA is doing to the machine, shown inline where it happened.
private struct ToolChip: View {
    let activity: ToolActivity

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Group {
                if activity.isRunning {
                    ProgressView().controlSize(.mini)
                } else {
                    Image(systemName: activity.isError
                        ? "exclamationmark.triangle.fill"
                        : "checkmark.circle.fill")
                        .foregroundStyle(activity.isError ? .orange : .green)
                }
            }
            .frame(width: 16)

            VStack(alignment: .leading, spacing: 2) {
                Text(activity.name)
                    .font(.footnote.monospaced().weight(.medium))
                if let summary = activity.summary {
                    Text(summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color(.tertiarySystemGroupedBackground), in: .rect(cornerRadius: 12))
    }
}

private struct ThinkingIndicator: View {
    @State private var phase = 0.0

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(.secondary)
                    .frame(width: 7, height: 7)
                    .opacity(0.35 + 0.65 * abs(sin(phase + Double(index) * 0.6)))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
        .task {
            // A timer rather than a repeating animation: the value is read
            // by three views and a phase offset is easier to reason about
            // than three staggered animations.
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(90))
                phase += 0.35
            }
        }
    }
}

private struct FailureRow: View {
    let error: APIError
    let canRetry: Bool
    let onRetry: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.circle.fill")
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 8) {
                Text(error.userMessage)
                    .font(.footnote)
                if canRetry {
                    Button("Try again", action: onRetry)
                        .font(.footnote.weight(.medium))
                        .buttonStyle(.bordered)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .card()
    }
}

// MARK: - Composer

private struct Composer: View {
    @Binding var draft: String
    @FocusState.Binding var isComposing: Bool
    let model: ChatModel
    let onSend: () -> Void

    @Environment(VoiceStore.self) private var voice

    var body: some View {
        VStack(spacing: 0) {
            Divider()

            if voice.isListening {
                HStack(spacing: 8) {
                    Image(systemName: "waveform")
                        .foregroundStyle(.red)
                        .symbolEffect(.variableColor.iterative)
                    Text(voice.transcript.isEmpty ? "Listening…" : voice.transcript)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16)
                .padding(.top, 10)
            }

            HStack(alignment: .bottom, spacing: 10) {
                MicrophoneButton(draft: $draft)

                TextField("Ask NOVA", text: $draft, axis: .vertical)
                    .font(.body)
                    .lineLimit(1...5)
                    .focused($isComposing)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(
                        Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 20)
                    )
                    .submitLabel(.send)
                    .onSubmit(onSend)

                if model.isStreaming {
                    Button {
                        model.cancel()
                    } label: {
                        Image(systemName: "stop.circle.fill")
                            .font(.system(size: 30))
                            .foregroundStyle(.secondary)
                    }
                    .accessibilityLabel("Stop")
                } else {
                    Button(action: onSend) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 30))
                    }
                    .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                    .accessibilityLabel("Send")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .background(.bar)
    }
}

/// Hold to talk.
///
/// Press-and-hold rather than tap-to-toggle: a microphone that is listening
/// because somebody forgot to tap it again is exactly the thing that makes
/// people distrust a device on their desk. Releasing puts the transcript in
/// the composer to be read and edited before it is sent — nothing is sent by
/// voice without being seen.
private struct MicrophoneButton: View {
    @Binding var draft: String
    @Environment(VoiceStore.self) private var voice

    var body: some View {
        Image(systemName: voice.isListening ? "mic.fill" : "mic")
            .font(.system(size: 22))
            .foregroundStyle(voice.isListening ? .red : .secondary)
            .frame(width: 44, height: 44)
            .contentShape(.rect)
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard !voice.isListening else { return }
                        Task { await voice.startListening() }
                    }
                    .onEnded { _ in
                        let heard = voice.stopListening()
                        guard !heard.isEmpty else { return }
                        draft = draft.isEmpty ? heard : "\(draft) \(heard)"
                    }
            )
            .accessibilityLabel("Hold to talk")
    }
}
