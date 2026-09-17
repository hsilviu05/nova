import Foundation
import UserNotifications

/// Which alerts have already been shown, so a poll that returns the same
/// unacknowledged alerts every fifteen seconds raises one banner, not one
/// per poll.
///
/// A plain value so it can be tested without the notification centre. It
/// forgets nothing until the app restarts, which is the right lifetime: an
/// alert acknowledged on the server and then re-raised is a new alert with a
/// new id.
struct AlertTracker: Sendable {
    private var seen: Set<UUID> = []

    /// The alerts not shown before, oldest first, and remembers them.
    mutating func unseen(in alerts: [Alert]) -> [Alert] {
        let fresh = alerts
            .filter { !seen.contains($0.id) }
            .sorted { $0.createdAt < $1.createdAt }
        seen.formUnion(fresh.map(\.id))
        return fresh
    }

    /// Mark everything currently known as seen without showing it: the
    /// alerts that were already on the server when the app opened are
    /// history, not news.
    mutating func prime(with alerts: [Alert]) {
        seen.formUnion(alerts.map(\.id))
    }
}

/// Somewhere a new alert goes besides the dashboard card.
protocol AlertPresenter: Sendable {
    func present(_ alert: Alert) async
}

/// A local notification, so the banner appears even while the dashboard is
/// on screen -- which, for a phone on a stand, is most of the time.
///
/// Local rather than push: no APNs, no key, no entitlement. The trade is
/// that the app has to be running to notice, and the README says so.
///
/// `@MainActor` because it is neither a value nor immutable: it holds the
/// notification centre, which is not `Sendable`, and a latch recording
/// whether authorisation has been asked for. `AlertPresenter` is `Sendable`,
/// and a class with mutable state cannot be -- isolating it to the main
/// actor is what makes the conformance true rather than asserted. The
/// dashboard that calls this is already on the main actor, so nothing hops.
@MainActor
final class LocalNotificationPresenter: NSObject, AlertPresenter, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private var asked = false

    override init() {
        super.init()
        center.delegate = self
    }

    func present(_ alert: Alert) async {
        if !asked {
            asked = true
            _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
        }
        let content = UNMutableNotificationContent()
        content.title = alert.isDown ? "\(alert.project) is down" : "\(alert.project) recovered"
        content.body = alert.message
        content.sound = alert.isDown ? .defaultCritical : .default
        content.interruptionLevel = alert.isDown ? .timeSensitive : .active
        let request = UNNotificationRequest(
            identifier: alert.id.uuidString, content: content, trigger: nil
        )
        try? await center.add(request)
    }

    // Shown as a banner even when the app is in the foreground. Without this
    // delegate method iOS suppresses foreground notifications entirely.
    //
    // `nonisolated` because iOS calls it from its own context and neither
    // parameter is `Sendable`. It reads nothing and returns a constant, so
    // there is no state to protect -- marking just this method is narrower
    // than a `@preconcurrency import`, which would relax the check for every
    // use of UserNotifications in the file including the ones that do touch
    // state.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }
}
