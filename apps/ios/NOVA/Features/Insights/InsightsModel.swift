import Foundation
import Observation

/// Drives the insights screen.
///
/// Loads the charts and the derived statements together, because they are two
/// views of the same window and fetching them separately would let the panels
/// disagree about what period they are describing.
@MainActor
@Observable
final class InsightsModel {
    /// Windows offered in the UI. The API accepts anything from 1 to 90 days;
    /// these are the ones worth a button.
    static let windows = [7, 14, 30, 90]

    private(set) var analytics: Analytics?
    private(set) var insights: Insights?
    private(set) var device: Device?
    private(set) var isLoading = false
    private(set) var hasLoaded = false
    var error: APIError?

    var windowDays = 30

    private let api: any NovaAPI

    init(api: any NovaAPI) {
        self.api = api
    }

    var isFirstLoad: Bool { !hasLoaded && isLoading }

    /// No device claimed yet: a different screen entirely from a device that
    /// has simply not reported much.
    var hasDevice: Bool { device != nil }

    /// Whether there is enough data to draw conclusions from. The server
    /// decides this, not the app -- the thresholds belong next to the
    /// statistics that use them.
    var isSufficient: Bool { analytics?.coverage.isSufficient ?? false }

    /// Why NOVA is declining to say anything, when it is.
    var insufficientReason: String? { insights?.insufficientReason }

    /// The heatmap grid, filled in. The API returns only non-empty cells,
    /// which is right for the wire and wrong for a chart: a grid with gaps
    /// renders as holes rather than as quiet hours.
    var weekGrid: [WeekdayHourBucket] {
        let counts = Dictionary(
            uniqueKeysWithValues: (analytics?.presenceByWeekdayHour ?? []).map {
                (WeekCell(weekday: $0.weekday, hour: $0.hour), $0.count)
            }
        )
        return (0..<7).flatMap { weekday in
            (0..<24).map { hour in
                WeekdayHourBucket(
                    weekday: weekday,
                    hour: hour,
                    count: counts[WeekCell(weekday: weekday, hour: hour)] ?? 0
                )
            }
        }
    }

    func load() async {
        isLoading = true
        defer {
            isLoading = false
            hasLoaded = true
        }

        do {
            // The device has to come first: everything else is scoped to it.
            let devices = try await api.devices()
            device = devices.first

            guard let device else {
                analytics = nil
                insights = nil
                error = nil
                return
            }

            analytics = try await api.analytics(
                deviceID: device.id, windowDays: windowDays
            )
            insights = try await api.insights(
                deviceID: device.id, windowDays: windowDays
            )
            error = nil
        } catch {
            // Keep whatever is on screen. A failed refresh should not blank a
            // working chart.
            self.error = (error as? APIError)
                ?? .undecodable(status: 0, underlying: "\(error)")
        }
    }
}

/// Key for filling the week grid. A struct rather than a tuple because
/// tuples are not `Hashable` in a way a dictionary will accept.
private struct WeekCell: Hashable {
    let weekday: Int
    let hour: Int
}

extension WeekdayHourBucket {
    /// Monday-first, matching the server's 0 = Monday convention.
    static let weekdayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    var weekdayName: String {
        Self.weekdayNames[min(max(weekday, 0), 6)]
    }
}
