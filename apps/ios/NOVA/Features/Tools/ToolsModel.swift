import Foundation
import Observation

/// Running tools by hand, and the confirmation that guards the dangerous ones.
///
/// The two-step handshake is the whole of this file. Invoking a destructive
/// tool answers 409 with a prompt and a token; the app shows the prompt, and
/// only sends the token if a person taps through. Nothing here can shortcut
/// that, because there is no other endpoint to call.
@MainActor
@Observable
final class ToolsModel {
    private(set) var tools: [NovaTool] = []
    private(set) var shellEnabled = false
    private(set) var isLoading = false
    private(set) var running: String?

    /// The call waiting for a yes. Presented as a sheet, dismissed by either
    /// answer, and never persisted -- a confirmation that outlived the
    /// question it answers is stale.
    var pending: PendingConfirmation?
    private(set) var lastResult: ToolRunResult?
    var error: APIError?

    var groups: [String] {
        Array(Set(tools.map(\.group))).sorted()
    }

    func tools(in group: String) -> [NovaTool] {
        tools.filter { $0.group == group }.sorted { $0.name < $1.name }
    }

    func load(using api: any NovaAPI) async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }

        do {
            let list = try await api.tools()
            tools = list.items
            shellEnabled = list.shellEnabled
            error = nil
        } catch let apiError as APIError {
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }

    /// Run a tool, or surface the confirmation it needs first.
    func run(
        _ tool: NovaTool,
        arguments: [String: String],
        using api: any NovaAPI
    ) async {
        await invoke(
            tool.name, arguments: arguments, token: nil, using: api
        )
    }

    /// Go ahead with a call the person has just approved.
    func confirm(_ confirmation: PendingConfirmation, using api: any NovaAPI) async {
        pending = nil
        await invoke(
            confirmation.tool,
            arguments: confirmation.arguments,
            token: confirmation.token,
            using: api
        )
    }

    func decline() {
        // The token is simply dropped. It expires on its own server-side,
        // and a declined action leaves no trace beyond the audit row for the
        // refusal, which is what should have happened.
        pending = nil
    }

    private func invoke(
        _ name: String,
        arguments: [String: String],
        token: String?,
        using api: any NovaAPI
    ) async {
        running = name
        error = nil
        lastResult = nil
        defer { running = nil }

        do {
            lastResult = try await api.invokeTool(
                name, arguments: arguments, confirmationToken: token
            )
        } catch let apiError as APIError {
            // A 409 is not a failure: it is the server asking a question.
            if var confirmation = PendingConfirmation(apiError) {
                confirmation.arguments = arguments
                pending = confirmation
            } else {
                error = apiError
            }
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }
}
