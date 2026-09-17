import SwiftUI

/// Chooses between the signed-out and signed-in worlds.
struct RootView: View {
    @Environment(SessionStore.self) private var session

    var body: some View {
        Group {
            switch session.state {
            case .restoring:
                // Deliberately not a sign-in screen: a returning user should
                // never see one flash before their session is restored.
                LaunchView()
            case .signedOut:
                SignInView()
            case let .signedIn(user):
                MainTabView(user: user)
            }
        }
        .animation(.easeInOut(duration: 0.25), value: session.state)
        .task {
            await session.restore()
        }
    }
}

private struct LaunchView: View {
    var body: some View {
        VStack(spacing: 20) {
            NovaMark(size: 72)
            ProgressView()
                .controlSize(.small)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
    }
}

/// The signed-in navigation.
///
/// Four tabs, in the order the questions get asked: what's the state of
/// things, talk to it, what does it know, what can it do. Settings is fifth
/// because it is opened once a month.
struct MainTabView: View {
    let user: User

    var body: some View {
        TabView {
            Tab("Dashboard", systemImage: "square.grid.2x2") {
                DashboardView()
            }
            Tab("Chat", systemImage: "bubble.left.and.bubble.right") {
                ChatView()
            }
            Tab("Memory", systemImage: "sparkles") {
                MemoryView()
            }
            Tab("Tools", systemImage: "wrench.and.screwdriver") {
                ToolsView()
            }
            Tab("Settings", systemImage: "gearshape") {
                SettingsView(user: user)
            }
        }
    }
}

/// NOVA's mark: a pair of marks that read as attention rather than a face.
struct NovaMark: View {
    /// The mark's geometry, as fractions of `size`.
    ///
    /// Named rather than inline because the app icon is the same mark, drawn
    /// by `Tools/make-appicon.swift` from these numbers. A change here means
    /// regenerating the icon, and `IconTests` is what says so out loud.
    enum Proportions {
        static let barWidth: CGFloat = 0.28
        static let barHeight: CGFloat = 0.52
        static let spacing: CGFloat = 0.22
        static let cornerRadius: CGFloat = 0.16
        /// Closed, the bars become a pair of slits rather than disappearing.
        static let asleepHeight: CGFloat = 0.08
    }

    var size: CGFloat = 56
    var isAwake: Bool = true

    var body: some View {
        HStack(spacing: size * Proportions.spacing) {
            bar
            bar
        }
        .frame(width: size, height: size * 0.62)
        .animation(.easeInOut(duration: 0.3), value: isAwake)
    }

    private var bar: some View {
        RoundedRectangle(cornerRadius: size * Proportions.cornerRadius, style: .continuous)
            .fill(Color.accentColor)
            .frame(
                width: size * Proportions.barWidth,
                height: size * (isAwake ? Proportions.barHeight : Proportions.asleepHeight)
            )
    }
}

#Preview("Launch") {
    LaunchView()
}
