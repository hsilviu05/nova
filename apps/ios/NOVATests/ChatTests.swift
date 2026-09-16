import Foundation
import Testing

@testable import NOVA

/// The chat model, driven by a scripted stream.
///
/// A fake `NovaAPI` and a hand-fed event sequence, because what is being
/// tested is how the transcript assembles -- deltas joining into one bubble,
/// tools landing between bubbles, a proposed action becoming a sheet rather
/// than an action. None of that needs a server, and testing it against one
/// would make it a test of the server.
@MainActor
struct ChatTests {
    // MARK: - Transcript assembly

    @Test("Deltas join into one bubble rather than one per fragment")
    func deltasAccumulate() {
        let model = ChatModel(api: StubAPI(), stream: unusedStream())

        model.apply(.delta("The API "))
        model.apply(.delta("is up."))

        #expect(model.transcript.count == 1)
        #expect(model.lastAssistantText == "The API is up.")
    }

    @Test("A tool closes the current bubble so the order reads as it happened")
    func toolSplitsTheReply() {
        // "Checking…" / [ran docker_containers] / "Three are running." is the
        // sequence a person watched; one bubble wrapped around the tool would
        // misreport it.
        let model = ChatModel(api: StubAPI(), stream: unusedStream())

        model.apply(.delta("Checking…"))
        model.apply(.toolStarted(callID: "c1", name: "docker_containers"))
        model.apply(.toolFinished(callID: "c1", name: "docker_containers", summary: "3 running", isError: false))
        model.apply(.delta("Three are running."))

        #expect(model.transcript.count == 3)
        if case let .tool(activity) = model.transcript[1] {
            #expect(activity.name == "docker_containers")
            #expect(activity.summary == "3 running")
            #expect(!activity.isRunning)
        } else {
            Issue.record("the middle item should be the tool")
        }
    }

    @Test("A running tool is shown as running until it returns")
    func toolIsPendingWhileItRuns() {
        let model = ChatModel(api: StubAPI(), stream: unusedStream())

        model.apply(.toolStarted(callID: "c1", name: "system_health"))

        guard case let .tool(activity) = model.transcript[0] else {
            Issue.record("expected a tool item")
            return
        }
        #expect(activity.isRunning)
        #expect(model.activity == .usingTool("system_health"))
    }

    @Test("A failing tool is marked, not hidden")
    func failedToolIsVisible() {
        // NOVA saying "SnapWorth is fine" after a check that failed is the
        // worst thing this app can do; the transcript has to show the failure.
        let model = ChatModel(api: StubAPI(), stream: unusedStream())

        model.apply(.toolStarted(callID: "c1", name: "project_health"))
        model.apply(
            .toolFinished(callID: "c1", name: "project_health", summary: "Not responding", isError: true)
        )

        guard case let .tool(activity) = model.transcript[0] else {
            Issue.record("expected a tool item")
            return
        }
        #expect(activity.isError)
    }

    // MARK: - Confirmations

    @Test("A proposed destructive action becomes a prompt, never an action")
    func confirmationIsRaised() {
        let model = ChatModel(api: StubAPI(), stream: unusedStream())

        model.apply(
            .confirmationNeeded(
                PendingConfirmation(
                    tool: "docker_remove_container",
                    prompt: "Remove the container “nova-api”?",
                    token: "t0ken",
                    arguments: ["container": "nova-api"]
                )
            )
        )

        let pending = model.pendingConfirmation
        #expect(pending?.tool == "docker_remove_container")
        #expect(pending?.prompt.contains("nova-api") == true)
    }

    @Test("Approving runs the tool with the token it was issued")
    func confirmingInvokesWithTheToken() async {
        // The token authorises exactly this call. Sending a different one --
        // or none -- is the bug this guards against.
        let api = StubAPI()
        let model = ChatModel(api: api, stream: unusedStream())
        model.pendingConfirmation = PendingConfirmation(
            tool: "docker_remove_container",
            prompt: "Remove it?",
            token: "t0ken",
            arguments: ["container": "nova-api"]
        )

        await model.confirm()

        #expect(api.invocations.count == 1)
        #expect(api.invocations[0].name == "docker_remove_container")
        #expect(api.invocations[0].token == "t0ken")
        #expect(api.invocations[0].arguments == ["container": "nova-api"])
        #expect(model.pendingConfirmation == nil)
    }

    @Test("Declining runs nothing")
    func decliningInvokesNothing() {
        let api = StubAPI()
        let model = ChatModel(api: api, stream: unusedStream())
        model.pendingConfirmation = PendingConfirmation(
            tool: "docker_remove_container", prompt: "Remove it?", token: "t0ken"
        )

        model.declineConfirmation()

        #expect(api.invocations.isEmpty)
        #expect(model.pendingConfirmation == nil)
    }

    @Test("What was approved lands in the transcript")
    func confirmedActionIsRecorded() async {
        // Otherwise the thread shows NOVA asking and then nothing, and there
        // is no record in the conversation that it happened.
        let model = ChatModel(api: StubAPI(), stream: unusedStream())
        model.pendingConfirmation = PendingConfirmation(
            tool: "docker_remove_container", prompt: "Remove it?", token: "t0ken"
        )

        await model.confirm()

        #expect(model.transcript.count == 1)
        if case let .tool(activity) = model.transcript[0] {
            #expect(activity.name == "docker_remove_container")
            #expect(!activity.isRunning)
        } else {
            Issue.record("expected a tool item")
        }
    }

    // MARK: - Failures

    @Test("A provider failure can be retried without retyping")
    func retryResendsTheLastMessage() {
        let model = ChatModel(api: StubAPI(), stream: unusedStream(), conversationID: UUID())

        model.send("Check SnapWorth")
        model.apply(.failed(code: "ai_unavailable", message: "NOVA is unreachable."))

        #expect(model.canRetry)
        #expect(model.lastSent == "Check SnapWorth")
    }

    @Test("Nothing to retry before anything was sent")
    func noRetryWithoutASend() {
        let model = ChatModel(api: StubAPI(), stream: unusedStream())
        #expect(!model.canRetry)
    }
}

// MARK: - Doubles

/// A `NovaAPI` that records what it was asked and answers plausibly.
@MainActor
private final class StubAPI: NovaAPI {
    struct Invocation: Sendable {
        let name: String
        let arguments: [String: String]
        let token: String?
    }

    nonisolated(unsafe) var invocations: [Invocation] = []

    nonisolated func invokeTool(
        _ name: String, arguments: [String: String], confirmationToken: String?
    ) async throws -> ToolRunResult {
        invocations.append(
            Invocation(name: name, arguments: arguments, token: confirmationToken)
        )
        return ToolRunResult(
            tool: name,
            content: "Removed nova-api.",
            data: .object([:]),
            isError: false,
            truncated: false,
            durationMs: 12
        )
    }

    // Everything below is unused by these tests and fails loudly rather than
    // returning something invented, so a test that starts depending on one
    // says so instead of passing for the wrong reason.
    nonisolated func register(email: String, password: String, displayName: String) async throws -> AuthenticatedUser { throw APIError.unauthenticated }
    nonisolated func signIn(email: String, password: String) async throws -> AuthenticatedUser { throw APIError.unauthenticated }
    nonisolated func signOut(refreshToken: String) async throws {}
    nonisolated func currentUser() async throws -> User { throw APIError.unauthenticated }
    nonisolated func updateProfile(displayName: String?, timezone: String?) async throws -> User { throw APIError.unauthenticated }
    nonisolated func conversations() async throws -> [Conversation] { [] }
    nonisolated func conversation(id: UUID) async throws -> ConversationDetail { throw APIError.unauthenticated }
    nonisolated func createConversation(title: String?) async throws -> Conversation { throw APIError.unauthenticated }
    nonisolated func deleteConversation(id: UUID) async throws {}
    nonisolated func sendMessage(_ content: String, to conversationID: UUID) async throws -> MessageExchange { throw APIError.unauthenticated }
    nonisolated func memories(category: MemoryCategory?, limit: Int, offset: Int) async throws -> MemoryPage { throw APIError.unauthenticated }
    nonisolated func searchMemories(_ query: String) async throws -> [MemorySearchResult] { [] }
    nonisolated func updateMemory(id: UUID, _ update: UpdateMemoryRequest) async throws -> Memory { throw APIError.unauthenticated }
    nonisolated func deleteMemory(id: UUID) async throws {}
    nonisolated func forgetEverything() async throws {}
    nonisolated func systemStatus() async throws -> SystemStatus { throw APIError.unauthenticated }
    nonisolated func activity(limit: Int) async throws -> ActivityPage { throw APIError.unauthenticated }
    nonisolated func tools() async throws -> ToolList { throw APIError.unauthenticated }
}

/// A stream client that is never actually driven.
///
/// The tests feed events into the model directly, because what is under test
/// is how they are folded into the transcript, not how they arrived.
@MainActor
private func unusedStream() -> ChatStreamClient {
    ChatStreamClient(configuration: .localDevelopment, tokens: TokenStore())
}
