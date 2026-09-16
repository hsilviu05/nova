import Foundation
import Observation

/// Keeps the dashboard current.
///
/// The phone sits on a desk, so this screen is looked at far more often than
/// it is interacted with. Two consequences shape everything here:
///
/// * It refreshes itself on a timer rather than only on appear, so a glance
///   shows the present rather than whenever it was last opened.
/// * A failed refresh keeps the last good reading and marks it stale. Wiping
///   the screen to an error because one poll timed out would make a phone on
///   a flaky Wi-Fi useless, when it has perfectly good information from
///   thirty seconds ago.
@MainActor
@Observable
final class DashboardModel {
    /// How often the dashboard re-reads while it is on screen.
    ///
    /// Fifteen seconds is a compromise: the status probes the model provider
    /// and every configured project, so it is not free, and nothing on this
    /// screen changes faster than that in a way anyone would notice.
    static let refreshInterval: Duration = .seconds(15)

    private(set) var status: SystemStatus?
    private(set) var lastUpdated: Date?
    private(set) var isRefreshing = false
    /// The last refresh failed. The reading above, if any, is older than it
    /// looks -- which the UI says rather than hiding.
    private(set) var isStale = false
    var error: APIError?

    private var ticker: Task<Void, Never>?

    var hasLoaded: Bool { status != nil }

    /// Whether NOVA is reachable at all, as distinct from whether what it
    /// reports is healthy. The two failures need different words.
    var isConnected: Bool {
        status != nil && !isStale
    }

    func load(using api: any NovaAPI) async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        do {
            status = try await api.systemStatus()
            lastUpdated = .now
            isStale = false
            error = nil
        } catch let apiError as APIError {
            error = apiError
            // Keep whatever was already on screen; it is still the most
            // recent thing NOVA actually said.
            isStale = true
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
            isStale = true
        }
    }

    /// Refresh on a timer for as long as the screen is on.
    ///
    /// Started from `.task`, which SwiftUI cancels when the view goes away,
    /// so nothing here polls in the background. That is deliberate: the app
    /// should not be asking a phone in someone's pocket to keep a connection
    /// open to their Mac.
    func startPolling(using api: any NovaAPI) {
        ticker?.cancel()
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                // Checked each turn rather than only at the start: if the
                // model goes away while the view is being torn down, this
                // stops instead of sleeping forever against a nil self.
                guard let self else { return }
                await self.load(using: api)

                do {
                    try await Task.sleep(for: Self.refreshInterval)
                } catch {
                    return  // cancelled
                }
            }
        }
    }

    func stopPolling() {
        ticker?.cancel()
        ticker = nil
    }
}
