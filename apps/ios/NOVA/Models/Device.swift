import Foundation

/// A NOVA belonging to the signed-in user.
struct Device: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let name: String?
    let model: String
    let firmwareVersion: String?
    let hardwareId: String
    /// Derived server-side from the last heartbeat, not stored, so it cannot
    /// go stale when a process dies without writing a final "offline".
    let isOnline: Bool
    let lastSeenAt: Date?
    let claimedAt: Date?
    let createdAt: Date

    var displayName: String {
        name ?? model
    }
}

/// One stored telemetry event.
struct TelemetryEvent: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let eventType: String
    /// The device's own clock. Distinct from `receivedAt` so a backlog
    /// flushed after an outage does not read as a burst of live activity.
    let recordedAt: Date
    let receivedAt: Date
    let batteryPercent: Int?
    let temperatureC: Double?
    let distanceCm: Int?
    let headYaw: Int?
    let headPitch: Int?
    let wifiRssi: Int?
    let uptimeSeconds: Int?
    let state: String?
    /// Event-specific fields that do not warrant a column server-side, such
    /// as `{"source": "time_of_flight"}` on a presence event.
    let payload: [String: JSONValue]

    /// Payload entries in a stable order, for display.
    var payloadPairs: [(key: String, value: String)] {
        payload
            .sorted { $0.key < $1.key }
            .map { ($0.key, $0.value.displayValue) }
    }
}

// MARK: - Requests

struct ClaimDeviceRequest: Encodable, Sendable {
    let code: String
    let name: String?
}

struct RenameDeviceRequest: Encodable, Sendable {
    let name: String
}

/// Acknowledgement that a command reached the device's socket.
///
/// Not that the servo moved: the device reports completion separately.
struct CommandAccepted: Decodable, Sendable {
    let commandId: UUID
    let accepted: Bool
    let detail: String?
}

/// A command the app can send to a connected device.
///
/// Ranges mirror the server's, so an out-of-range value is caught before a
/// round trip. The server re-validates regardless — this is convenience, not
/// the enforcement point.
enum DeviceCommand: Encodable, Sendable {
    case headMove(yaw: Int, pitch: Int, durationMs: Int = 500)
    case expression(Expression, intensity: Double = 1.0)
    case speak(text: String)
    case restart

    enum Expression: String, Encodable, Sendable, CaseIterable {
        case idle, listening, thinking, speaking
        case curious, happy, confused, alert, sleeping
    }

    static let yawRange = -90...90
    static let pitchRange = -45...45

    private enum CodingKeys: String, CodingKey {
        case command, payload
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)

        switch self {
        case let .headMove(yaw, pitch, durationMs):
            try container.encode("head.move", forKey: .command)
            try container.encode(
                [
                    "yaw": yaw.clamped(to: Self.yawRange),
                    "pitch": pitch.clamped(to: Self.pitchRange),
                    "duration_ms": durationMs,
                ],
                forKey: .payload
            )

        case let .expression(emotion, intensity):
            try container.encode("expression.set", forKey: .command)
            var payload = container.nestedContainer(
                keyedBy: ExpressionKeys.self, forKey: .payload
            )
            try payload.encode(emotion, forKey: .emotion)
            try payload.encode(min(max(intensity, 0), 1), forKey: .intensity)

        case let .speak(text):
            try container.encode("speaker.play", forKey: .command)
            var payload = container.nestedContainer(
                keyedBy: SpeakKeys.self, forKey: .payload
            )
            try payload.encode(text, forKey: .text)

        case .restart:
            try container.encode("device.restart", forKey: .command)
            try container.encode([String: String](), forKey: .payload)
        }
    }

    private enum ExpressionKeys: String, CodingKey {
        case emotion, intensity
    }

    private enum SpeakKeys: String, CodingKey {
        case text
    }
}

private extension Int {
    func clamped(to range: ClosedRange<Int>) -> Int {
        Swift.min(Swift.max(self, range.lowerBound), range.upperBound)
    }
}
