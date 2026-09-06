import Foundation
import Observation

/// The most recent vitals, folded out of the telemetry stream.
///
/// Telemetry is append-only and each event carries only what changed, so the
/// current picture is assembled from the newest event that mentions each
/// field rather than from any single row.
struct Vitals: Equatable, Sendable {
    var battery: Int?
    var temperature: Double?
    var signalStrength: Int?
    var uptime: Int?
    var state: String?
    var lastDistance: Int?

    var signalDescription: String {
        guard let rssi = signalStrength else { return "—" }
        return switch rssi {
        case (-55)...: "Excellent"
        case (-70)..<(-55): "Good"
        case (-80)..<(-70): "Fair"
        default: "Weak"
        }
    }

    var uptimeDescription: String {
        guard let uptime else { return "—" }
        let hours = uptime / 3600
        let minutes = (uptime % 3600) / 60
        return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
    }

    /// Fold a newest-first telemetry list into a current picture.
    static func from(_ events: [TelemetryEvent]) -> Vitals {
        var vitals = Vitals()

        // Newest first, so the first non-nil value for each field wins and
        // older events never overwrite fresher ones.
        for event in events {
            vitals.battery = vitals.battery ?? event.batteryPercent
            vitals.temperature = vitals.temperature ?? event.temperatureC
            vitals.signalStrength = vitals.signalStrength ?? event.wifiRssi
            vitals.uptime = vitals.uptime ?? event.uptimeSeconds
            vitals.state = vitals.state ?? event.state
            vitals.lastDistance = vitals.lastDistance ?? event.distanceCm
        }
        return vitals
    }
}

/// Loads the account's devices and the newest telemetry for the primary one.
@MainActor
@Observable
final class DeviceListModel {
    private(set) var devices: [Device] = []
    private(set) var vitals = Vitals()
    private(set) var recentEvents: [TelemetryEvent] = []
    private(set) var hasLoaded = false
    private(set) var isLoading = false
    var error: APIError?

    var isFirstLoad: Bool { !hasLoaded && isLoading }

    /// V1 is a single-device product; the first claimed device is *the*
    /// device. The list is kept so a second one is a UI change, not a model
    /// change.
    var primary: Device? { devices.first }

    /// A short observation to show on the home screen.
    ///
    /// Deliberately sparse: a companion that comments on everything stops
    /// being noticed. Only genuinely notable states produce a line.
    var latestRemark: String? {
        guard let device = primary else { return nil }

        if !device.isOnline {
            return device.lastSeenAt == nil
                ? "We haven't met yet. Plug me in."
                : "I lost the connection."
        }
        if let battery = vitals.battery, battery < 20 {
            return "Running low. \(battery)% left."
        }
        if let distance = vitals.lastDistance, distance < 60 {
            return "You're close. I noticed."
        }
        return nil
    }

    func load(using api: any NovaAPI) async {
        isLoading = true
        defer {
            isLoading = false
            hasLoaded = true
        }

        do {
            devices = try await api.devices()
            error = nil

            guard let primary else {
                vitals = Vitals()
                recentEvents = []
                return
            }

            recentEvents = try await api.telemetry(
                deviceID: primary.id, limit: 50, eventType: nil
            )
            vitals = Vitals.from(recentEvents)
        } catch let apiError as APIError {
            // Keep whatever was already on screen: a failed refresh should
            // not blank out a working view.
            error = apiError
        } catch {
            self.error = .undecodable(status: 0, underlying: "\(error)")
        }
    }
}
