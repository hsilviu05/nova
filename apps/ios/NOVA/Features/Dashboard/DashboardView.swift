import SwiftUI

/// What NOVA can see, at a glance.
///
/// Designed for a phone on a stand two feet away rather than one held in a
/// hand: the verdict is one line in a large weight at the top, and everything
/// below it is detail for when the verdict says to look closer. The ordering
/// is by how often a question is actually asked -- is it up, what is running,
/// what did it just do -- not by how much data each card has.
struct DashboardView: View {
    @Environment(\.novaAPI) private var api
    @Environment(\.scenePhase) private var scenePhase
    @State private var model = DashboardModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                if let status = model.status {
                    content(status)
                } else if model.isRefreshing {
                    ProgressView()
                        .controlSize(.large)
                        .frame(maxWidth: .infinity, minHeight: 320)
                } else {
                    UnreachableView(error: model.error)
                        .frame(maxWidth: .infinity, minHeight: 320)
                }
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("NOVA")
            .refreshable { await model.load(using: api) }
        }
        .task {
            model.startPolling(using: api)
        }
        .onDisappear { model.stopPolling() }
        .onChange(of: scenePhase) { _, phase in
            // Polling stops when the app is backgrounded and resumes on
            // return. A phone on a stand spends most of its life with the
            // screen off, and a timer running through that is a battery
            // drain buying nothing anyone can see.
            if phase == .active {
                model.startPolling(using: api)
            } else {
                model.stopPolling()
            }
        }
    }

    private func content(_ status: SystemStatus) -> some View {
        VStack(spacing: 16) {
            VerdictCard(status: status, isStale: model.isStale, updated: model.lastUpdated)
            AICard(ai: status.ai)
            HostCard(host: status.host, dependencies: status.dependencies)

            if !status.projects.isEmpty {
                ProjectsCard(projects: status.projects)
            }

            MemoryCard(memory: status.memory)
            ToolsCard(tools: status.tools)

            if !status.recentActivity.isEmpty {
                ActivityCard(entries: Array(status.recentActivity.prefix(5)))
            }
        }
        .padding(16)
    }
}

// MARK: - Cards

/// The one line worth reading from across a desk.
private struct VerdictCard: View {
    let status: SystemStatus
    let isStale: Bool
    let updated: Date?

    private var symbol: String {
        if isStale { return "wifi.exclamationmark" }
        return status.isHealthy ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
    }

    private var tint: Color {
        if isStale { return .orange }
        return status.isHealthy ? .green : .orange
    }

    private var headline: String {
        if isStale { return "Can't reach NOVA" }
        return status.isHealthy ? "Everything's fine" : "Something needs a look"
    }

    var body: some View {
        VStack(spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: symbol)
                    .font(.system(size: 34))
                    .foregroundStyle(tint)
                Text(headline)
                    .font(.title2.weight(.semibold))
                Spacer(minLength: 0)
            }

            if !isStale, !status.problems.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(status.problems, id: \.self) { problem in
                        Label(problem, systemImage: "circle.fill")
                            .labelStyle(BulletLabel())
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            if let updated {
                HStack {
                    Spacer()
                    Text(
                        isStale
                            ? "Last seen \(updated, format: .relative(presentation: .named))"
                            : "Updated \(updated, format: .relative(presentation: .named))"
                    )
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(18)
        .card()
    }
}

private struct AICard: View {
    let ai: AIStatus

    var body: some View {
        Card(title: "AI", symbol: "brain") {
            VStack(alignment: .leading, spacing: 10) {
                StatusLine(
                    label: ai.model,
                    detail: ai.provider,
                    isGood: ai.online && !ai.isOffline
                )

                if let detail = ai.detail {
                    Text(detail)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                HStack(spacing: 16) {
                    if let latency = ai.latencyMs {
                        Metric(value: "\(Int(latency)) ms", label: "latency")
                    }
                    Metric(value: ai.supportsTools ? "Yes" : "No", label: "tools")
                }
            }
        }
    }
}

private struct HostCard: View {
    let host: HostStatus
    let dependencies: [DependencyStatus]

    var body: some View {
        Card(title: "System", symbol: "desktopcomputer") {
            VStack(alignment: .leading, spacing: 12) {
                Text("\(host.hostname) · \(host.platform)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                HStack(spacing: 18) {
                    Metric(
                        value: host.loadPerCore.map { String(format: "%.2f", $0) } ?? "—",
                        label: "load / core"
                    )
                    Metric(
                        value: host.memoryPercentUsed.map { "\(Int($0))%" } ?? "—",
                        label: "memory"
                    )
                    Metric(
                        value: host.diskPercentUsed.map { "\(Int($0))%" } ?? "—",
                        label: "disk"
                    )
                }

                ForEach(dependencies) { dependency in
                    StatusLine(
                        label: dependency.name.capitalized,
                        detail: dependency.latencyMs.map { "\(Int($0)) ms" } ?? dependency.error,
                        isGood: dependency.healthy
                    )
                }
            }
        }
    }
}

private struct ProjectsCard: View {
    let projects: [ProjectStatus]

    var body: some View {
        Card(title: "Projects", symbol: "shippingbox") {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(projects) { project in
                    VStack(alignment: .leading, spacing: 4) {
                        StatusLine(
                            label: project.name,
                            detail: project.summary,
                            isGood: project.healthy
                        )
                        if !project.dependencies.isEmpty {
                            Text(
                                project.dependencies
                                    .sorted { $0.key < $1.key }
                                    .map { "\($0.key) \($0.value ? "✓" : "✗")" }
                                    .joined(separator: "  ")
                            )
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                        }
                    }
                }
            }
        }
    }
}

private struct MemoryCard: View {
    let memory: MemoryStatus

    var body: some View {
        Card(title: "Memory", symbol: "sparkles") {
            VStack(alignment: .leading, spacing: 10) {
                Metric(value: String(memory.total), label: memory.total == 1 ? "thing remembered" : "things remembered")

                ForEach(memory.recent, id: \.self) { line in
                    Text(line)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}

private struct ToolsCard: View {
    let tools: ToolsStatus

    var body: some View {
        Card(title: "Tools", symbol: "wrench.and.screwdriver") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 18) {
                    Metric(value: String(tools.count), label: "available")
                    Metric(value: String(tools.invocationsToday), label: "run today")
                    if tools.failuresToday > 0 {
                        Metric(value: String(tools.failuresToday), label: "failed", tint: .orange)
                    }
                }

                if !tools.groups.isEmpty {
                    Text(tools.groups.joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }

                if tools.shellEnabled {
                    // Said out loud, every time. Shell execution is off by
                    // default, and somebody should never be unsure whether
                    // it is on.
                    Label("Shell execution is enabled", systemImage: "exclamationmark.triangle")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.orange)
                }
            }
        }
    }
}

private struct ActivityCard: View {
    let entries: [ActivityEntry]

    var body: some View {
        Card(title: "Recent activity", symbol: "clock.arrow.circlepath") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(entries, id: \.id) { entry in
                    ActivityRow(entry: entry)
                }
            }
        }
    }
}

/// One line of the audit log.
///
/// Extracted rather than inlined: the icon distinguishes a tool the model
/// chose from one a person pressed, which is the first thing anyone reviewing
/// this wants to know, and it is worth a named view.
private struct ActivityRow: View {
    let entry: ActivityEntry

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: entry.initiatedByModel ? "brain" : "hand.tap")
                .font(.caption)
                .foregroundStyle(.tertiary)
            Text(entry.toolName)
                .font(.footnote.monospaced())
            Spacer(minLength: 4)
            Text(entry.outcome)
                .font(.caption)
                .foregroundStyle(entry.succeeded ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
            Text(entry.createdAt, format: .relative(presentation: .numeric))
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
    }
}

// MARK: - Shared pieces

/// Shown when there is nothing to show, which on a local network is a
/// routine state rather than an exceptional one.
struct UnreachableView: View {
    let error: APIError?

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 42))
                .foregroundStyle(.secondary)
            Text("NOVA server unavailable")
                .font(.title3.weight(.semibold))
            Text(error?.userMessage ?? "Nothing answered at the configured address.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Text("Check the address in Settings, and that NOVA is running on your Mac.")
                .font(.footnote)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
        }
    }
}

struct Card<Content: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: symbol)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .card()
    }
}

struct StatusLine: View {
    let label: String
    var detail: String?
    let isGood: Bool

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(isGood ? Color.green : Color.orange)
                .frame(width: 9, height: 9)
            Text(label)
                .font(.callout.weight(.medium))
            Spacer(minLength: 4)
            if let detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct Metric: View {
    let value: String
    let label: String
    var tint: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.title3.weight(.medium))
                .monospacedDigit()
                .foregroundStyle(tint)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

/// A dot instead of SF Symbols' default bullet, which is too loud at this size.
struct BulletLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            configuration.icon.font(.system(size: 5))
            configuration.title
        }
    }
}

extension View {
    /// The one card treatment, so every surface in the app matches.
    func card() -> some View {
        background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 16))
    }
}

struct InlineError: View {
    let error: APIError

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.circle")
                .foregroundStyle(.orange)
            Text(error.userMessage)
                .font(.footnote)
            Spacer(minLength: 0)
        }
        .padding(12)
        .card()
    }
}
