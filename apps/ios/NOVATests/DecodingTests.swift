import Foundation
import Testing

@testable import NOVA

/// Decoding against payloads captured from a running NOVA API, not invented.
///
/// The fixtures below are literal responses recorded from the server, so a
/// change in the API contract shows up here rather than at runtime.
struct DecodingTests {
    // MARK: - Timestamps

    @Test("Parses the microsecond timestamps the API actually emits")
    func fractionalSeconds() throws {
        // The API serialises Python datetimes, which carry microseconds.
        // JSONDecoder's built-in .iso8601 strategy cannot parse these, which
        // would fail on essentially every response.
        let date = try #require(ISO8601.date(from: "2026-09-06T15:42:28.579713Z"))

        let parts = Calendar(identifier: .gregorian).dateComponents(
            in: TimeZone(identifier: "UTC")!, from: date
        )
        #expect(parts.year == 2026)
        #expect(parts.month == 9)
        #expect(parts.day == 6)
        #expect(parts.hour == 15)
        #expect(parts.minute == 42)
        #expect(parts.second == 28)
    }

    @Test("Parses a timestamp that landed on a whole second")
    func withoutFractionalSeconds() throws {
        // Rare but real: a timestamp with microsecond == 0 serialises without
        // a fractional part, and would otherwise crash exactly once in a
        // million responses.
        #expect(ISO8601.date(from: "2026-09-06T15:42:28Z") != nil)
    }

    @Test("Rejects text that is not a timestamp")
    func rejectsGarbage() {
        #expect(ISO8601.date(from: "not a date") == nil)
        #expect(ISO8601.date(from: "") == nil)
    }

    // MARK: - Session

    @Test("Decodes the register/sign-in response")
    func authenticatedUser() throws {
        let json = """
        {
          "user": {
            "id": "113bf44d-995e-4cfe-b519-2589519e1d80",
            "email": "someone@example.com",
            "display_name": "Silviu",
            "is_active": true,
            "created_at": "2026-09-06T15:42:28.579713Z",
            "last_login_at": "2026-09-06T15:42:28.625180Z"
          },
          "tokens": {
            "access_token": "header.payload.signature",
            "refresh_token": "IZvj3IvPwfzV6xKH--epYNLCXNpJqR3x",
            "token_type": "bearer",
            "expires_in": 899
          }
        }
        """

        let result = try JSONCoding.decoder.decode(
            AuthenticatedUser.self, from: Data(json.utf8)
        )

        #expect(result.user.displayName == "Silviu")
        #expect(result.user.isActive)
        #expect(result.user.lastLoginAt != nil)
        #expect(result.tokens.tokenType == "bearer")
        #expect(result.tokens.expiresIn == 899)
    }

    @Test("Computes an access-token expiry from the issued lifetime")
    func tokenExpiry() {
        let pair = TokenPair(
            accessToken: "a", refreshToken: "r", tokenType: "bearer", expiresIn: 900
        )
        let now = Date(timeIntervalSince1970: 1_000_000)

        #expect(pair.expiry(from: now).timeIntervalSince(now) == 900)
    }

    // MARK: - Devices

    @Test("Decodes a claimed device, including its null fields")
    func device() throws {
        let json = """
        {
          "id": "dc8608e9-f102-4e72-af8e-f15a1abd7259",
          "name": "Nova",
          "model": "ESP32-S3-Touch-AMOLED-2.06",
          "firmware_version": "0.1.0",
          "hardware_id": "esp32s3-838215147",
          "is_online": false,
          "last_seen_at": null,
          "claimed_at": "2026-09-06T15:42:28.699214Z",
          "created_at": "2026-09-06T15:42:28.664075Z"
        }
        """

        let device = try JSONCoding.decoder.decode(Device.self, from: Data(json.utf8))

        #expect(device.name == "Nova")
        #expect(device.isOnline == false)
        // A freshly claimed device has never reported in.
        #expect(device.lastSeenAt == nil)
        #expect(device.claimedAt != nil)
    }

    @Test("Falls back to the model when a device has no name")
    func unnamedDevice() throws {
        let json = """
        {
          "id": "dc8608e9-f102-4e72-af8e-f15a1abd7259",
          "name": null,
          "model": "ESP32-S3-Touch-AMOLED-2.06",
          "firmware_version": null,
          "hardware_id": "esp32s3-1",
          "is_online": true,
          "last_seen_at": "2026-09-06T15:42:28.579713Z",
          "claimed_at": "2026-09-06T15:42:28.699214Z",
          "created_at": "2026-09-06T15:42:28.664075Z"
        }
        """

        let device = try JSONCoding.decoder.decode(Device.self, from: Data(json.utf8))
        #expect(device.displayName == "ESP32-S3-Touch-AMOLED-2.06")
    }

    @Test("Decodes a telemetry event with sparse fields")
    func telemetry() throws {
        // Events carry only what changed, so most columns are null.
        let json = """
        [{
          "id": "aa8608e9-f102-4e72-af8e-f15a1abd7259",
          "event_type": "person_detected",
          "recorded_at": "2026-09-06T15:42:28.579713Z",
          "received_at": "2026-09-06T15:42:28.680000Z",
          "battery_percent": null,
          "temperature_c": null,
          "distance_cm": 72,
          "head_yaw": 14,
          "head_pitch": null,
          "wifi_rssi": null,
          "uptime_seconds": null,
          "state": "CURIOUS",
          "payload": {"source": "time_of_flight"}
        }]
        """

        let events = try JSONCoding.decoder.decode(
            [TelemetryEvent].self, from: Data(json.utf8)
        )

        #expect(events.count == 1)
        #expect(events[0].distanceCm == 72)
        #expect(events[0].batteryPercent == nil)
        // recorded_at and received_at are distinct on purpose: a backlog
        // flushed after an outage must not read as live activity.
        #expect(events[0].recordedAt != events[0].receivedAt)
        // Event-specific fields survive rather than being dropped.
        #expect(events[0].payload["source"]?.stringValue == "time_of_flight")
    }

    @Test("Decodes a payload of mixed JSON types")
    func mixedPayload() throws {
        let json = """
        {
          "id": "aa8608e9-f102-4e72-af8e-f15a1abd7259",
          "event_type": "motion",
          "recorded_at": "2026-09-06T15:42:28.579713Z",
          "received_at": "2026-09-06T15:42:28.680000Z",
          "battery_percent": null, "temperature_c": null, "distance_cm": null,
          "head_yaw": null, "head_pitch": null, "wifi_rssi": null,
          "uptime_seconds": null, "state": null,
          "payload": {
            "picked_up": true, "tilt_degrees": 32.5,
            "axis": "y", "nothing": null
          }
        }
        """

        let event = try JSONCoding.decoder.decode(
            TelemetryEvent.self, from: Data(json.utf8)
        )

        #expect(event.payload["picked_up"] == .bool(true))
        #expect(event.payload["tilt_degrees"] == .number(32.5))
        #expect(event.payload["axis"] == .string("y"))
        #expect(event.payload["nothing"] == .null)
    }

    @Test("An empty payload decodes to an empty dictionary, not a failure")
    func emptyPayload() throws {
        let json = """
        {
          "id": "aa8608e9-f102-4e72-af8e-f15a1abd7259",
          "event_type": "heartbeat",
          "recorded_at": "2026-09-06T15:42:28.579713Z",
          "received_at": "2026-09-06T15:42:28.680000Z",
          "battery_percent": 78, "temperature_c": null, "distance_cm": null,
          "head_yaw": null, "head_pitch": null, "wifi_rssi": null,
          "uptime_seconds": null, "state": "IDLE",
          "payload": {}
        }
        """

        let event = try JSONCoding.decoder.decode(
            TelemetryEvent.self, from: Data(json.utf8)
        )
        #expect(event.payload.isEmpty)
    }

    // MARK: - Errors

    @Test("Decodes the shared error envelope")
    func errorEnvelope() throws {
        let json = """
        {
          "error": {
            "code": "missing_credentials",
            "message": "Authorization header is missing.",
            "request_id": "24897484-e6d8-4508-80b3-8820eaf04b98",
            "details": {}
          }
        }
        """

        let envelope = try JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: Data(json.utf8)
        )

        #expect(envelope.error.code == "missing_credentials")
        #expect(envelope.error.requestId != nil)
    }

    @Test("Surfaces per-field validation messages")
    func validationErrors() throws {
        let json = """
        {
          "error": {
            "code": "validation_error",
            "message": "The request payload is invalid.",
            "request_id": "24897484-e6d8-4508-80b3-8820eaf04b98",
            "details": {
              "errors": [
                {
                  "type": "value_error",
                  "loc": ["body", "email"],
                  "msg": "value is not a valid email address"
                },
                {
                  "type": "string_too_short",
                  "loc": ["body", "password"],
                  "msg": "String should have at least 12 characters"
                }
              ]
            }
          }
        }
        """

        let envelope = try JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: Data(json.utf8)
        )
        let error = APIError.api(status: 422, envelope: envelope.error)

        #expect(error.fieldErrors["email"] != nil)
        #expect(error.fieldErrors["password"] != nil)
        #expect(error.code == "validation_error")
    }

    @Test("Validation errors never carry the submitted value")
    func validationErrorsDoNotEchoInput() throws {
        // The server drops Pydantic's `input` field precisely so a rejected
        // password is not reflected back. If that ever regresses, the client
        // should not start relying on it.
        let json = """
        {
          "error": {
            "code": "validation_error",
            "message": "The request payload is invalid.",
            "request_id": "r",
            "details": {
              "errors": [
                {"type": "x", "loc": ["body", "password"], "msg": "too short"}
              ]
            }
          }
        }
        """

        let envelope = try JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: Data(json.utf8)
        )
        let field = try #require(envelope.error.details?.errors?.first)

        #expect(field.msg == "too short")
        #expect(field.field == "password")
    }

    @Test("A 5xx is retryable, a 4xx is not")
    func retryability() {
        func envelope(_ code: String) -> APIErrorEnvelope.Detail {
            APIErrorEnvelope.Detail(
                code: code, message: "", requestId: nil, details: nil
            )
        }

        #expect(APIError.api(status: 503, envelope: envelope("x")).isRetryable)
        #expect(APIError.api(status: 429, envelope: envelope("x")).isRetryable)
        #expect(!APIError.api(status: 404, envelope: envelope("x")).isRetryable)
        #expect(!APIError.unauthenticated.isRetryable)
    }
}
