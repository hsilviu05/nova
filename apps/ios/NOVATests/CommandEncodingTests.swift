import Foundation
import Testing

@testable import NOVA

/// Command payloads must match what the server's discriminated union accepts.
///
/// The server rejects anything with an unexpected field or an out-of-range
/// value, so a mismatch here is a 422 at runtime.
struct CommandEncodingTests {
    private func encode(_ command: DeviceCommand) throws -> [String: Any] {
        let data = try JSONEncoder().encode(command)
        let object = try JSONSerialization.jsonObject(with: data)
        return try #require(object as? [String: Any])
    }

    @Test("head.move carries the shape the server expects")
    func headMove() throws {
        let json = try encode(.headMove(yaw: 20, pitch: -5, durationMs: 500))

        #expect(json["command"] as? String == "head.move")
        let payload = try #require(json["payload"] as? [String: Any])
        #expect(payload["yaw"] as? Int == 20)
        #expect(payload["pitch"] as? Int == -5)
        #expect(payload["duration_ms"] as? Int == 500)
    }

    @Test("Head movement is clamped to the mechanism's travel")
    func clampsMovement() throws {
        // The server rejects rather than clamps, deliberately. Clamping here
        // means the app never sends a request it knows will fail, while the
        // server stays the enforcement point.
        let json = try encode(.headMove(yaw: 999, pitch: -999))
        let payload = try #require(json["payload"] as? [String: Any])

        #expect(payload["yaw"] as? Int == DeviceCommand.yawRange.upperBound)
        #expect(payload["pitch"] as? Int == DeviceCommand.pitchRange.lowerBound)
    }

    @Test("expression.set encodes a known emotion")
    func expression() throws {
        let json = try encode(.expression(.curious, intensity: 0.8))

        #expect(json["command"] as? String == "expression.set")
        let payload = try #require(json["payload"] as? [String: Any])
        #expect(payload["emotion"] as? String == "curious")
        #expect((payload["intensity"] as? Double) == 0.8)
    }

    @Test("Expression intensity stays within 0...1")
    func clampsIntensity() throws {
        let json = try encode(.expression(.happy, intensity: 5))
        let payload = try #require(json["payload"] as? [String: Any])

        #expect((payload["intensity"] as? Double) == 1.0)
    }

    @Test("Every emotion matches one the server accepts")
    func emotionsMatchServer() {
        // Mirrors the Literal in nova/schemas/protocol.py. A value the server
        // does not know is a 422.
        let serverEmotions: Set<String> = [
            "idle", "listening", "thinking", "speaking",
            "curious", "happy", "confused", "alert", "sleeping",
        ]
        let appEmotions = Set(DeviceCommand.Expression.allCases.map(\.rawValue))

        #expect(appEmotions == serverEmotions)
    }

    @Test("device.restart sends an empty payload, not a missing one")
    func restart() throws {
        let json = try encode(.restart)

        #expect(json["command"] as? String == "device.restart")
        // The server's schema requires the key to be present.
        #expect(json["payload"] != nil)
    }

    @Test("speaker.play carries the text")
    func speak() throws {
        let json = try encode(.speak(text: "You're back."))

        #expect(json["command"] as? String == "speaker.play")
        let payload = try #require(json["payload"] as? [String: Any])
        #expect(payload["text"] as? String == "You're back.")
    }
}

/// Folding a telemetry stream into a current picture.
struct VitalsTests {
    private func event(
        battery: Int? = nil,
        temperature: Double? = nil,
        rssi: Int? = nil,
        state: String? = nil,
        recordedAt: Date = .now
    ) -> TelemetryEvent {
        TelemetryEvent(
            id: UUID(),
            eventType: "heartbeat",
            recordedAt: recordedAt,
            receivedAt: recordedAt,
            batteryPercent: battery,
            temperatureC: temperature,
            distanceCm: nil,
            headYaw: nil,
            headPitch: nil,
            wifiRssi: rssi,
            uptimeSeconds: nil,
            state: state,
            payload: [:]
        )
    }

    @Test("Takes the newest value for each field")
    func newestWins() {
        // The list arrives newest-first, and events carry only what changed,
        // so an older reading must never overwrite a fresher one.
        let vitals = Vitals.from([
            event(battery: 78),
            event(battery: 42, temperature: 30),
        ])

        #expect(vitals.battery == 78)
        // Filled from the older event, because the newer one did not report it.
        #expect(vitals.temperature == 30)
    }

    @Test("An empty stream yields nothing rather than zeroes")
    func empty() {
        let vitals = Vitals.from([])

        #expect(vitals.battery == nil)
        #expect(vitals.signalDescription == "—")
        #expect(vitals.uptimeDescription == "—")
    }

    @Test(
        "Describes signal strength in words",
        arguments: [(-40, "Excellent"), (-60, "Good"), (-75, "Fair"), (-95, "Weak")]
    )
    func signalDescription(rssi: Int, expected: String) {
        #expect(Vitals.from([event(rssi: rssi)]).signalDescription == expected)
    }
}
