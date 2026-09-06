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
struct MainTabView: View {
    let user: User

    var body: some View {
        TabView {
            Tab("Home", systemImage: "house") {
                HomeView()
            }
            Tab("Chat", systemImage: "bubble.left.and.bubble.right") {
                ChatView()
            }
            Tab("Memory", systemImage: "brain") {
                MemoryView()
            }
            Tab("Insights", systemImage: "chart.bar") {
                InsightsView()
            }
            Tab("Devices", systemImage: "cpu") {
                DeviceListView()
            }
            Tab("Settings", systemImage: "gearshape") {
                SettingsView(user: user)
            }
        }
    }
}

/// NOVA's mark: a pair of eyes, the same idea the device shows on its face.
struct NovaMark: View {
    var size: CGFloat = 56
    var isAwake: Bool = true

    var body: some View {
        HStack(spacing: size * 0.22) {
            eye
            eye
        }
        .frame(width: size, height: size * 0.62)
        .animation(.easeInOut(duration: 0.3), value: isAwake)
    }

    private var eye: some View {
        RoundedRectangle(cornerRadius: size * 0.16, style: .continuous)
            .fill(Color.accentColor)
            .frame(width: size * 0.28, height: isAwake ? size * 0.52 : size * 0.08)
    }
}

#Preview("Launch") {
    LaunchView()
}
