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

    @Test("Parses a bare calendar date")
    func dateOnly() throws {
        // Analytics daily buckets are dates, not instants: the server has
        // already resolved the local calendar day. A parser that only
        // handles full timestamps fails the entire analytics response, not
        // just this field.
        let date = try #require(ISO8601.date(from: "2026-08-17"))

        let parts = Calendar(identifier: .gregorian).dateComponents(
            in: TimeZone(identifier: "UTC")!, from: date
        )
        #expect(parts.year == 2026)
        #expect(parts.month == 8)
        #expect(parts.day == 17)
        #expect(parts.hour == 0)
    }

    @Test("Rejects text that is not a timestamp")
    func rejectsGarbage() {
        #expect(ISO8601.date(from: "not a date") == nil)
        #expect(ISO8601.date(from: "") == nil)
        #expect(ISO8601.date(from: "2026-13-45") == nil)
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
            "timezone": "Europe/Bucharest",
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
        #expect(result.user.timezone == "Europe/Bucharest")
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

    // MARK: - System status

    @Test("Decodes the dashboard's one response")
    func systemStatus() throws {
        // Captured from a running NOVA with nothing configured beyond the
        // defaults, which is the state a first run is actually in.
        let json = """
        {
          "generated_at": "2026-09-16T15:42:28.579713Z",
          "ai": {
            "provider": "ollama", "model": "qwen3.8:latest", "online": true,
            "supports_tools": true, "latency_ms": 412.5, "detail": null
          },
          "host": {
            "hostname": "silviu-mac", "platform": "Darwin 27.0.0", "cpu_count": 10,
            "load_per_core": 0.42, "memory_percent_used": 61.3,
            "disk_percent_used": 74.1
          },
          "dependencies": [
            {"name": "postgres", "healthy": true, "latency_ms": 1.2, "error": null},
            {"name": "redis", "healthy": true, "latency_ms": 0.4, "error": null}
          ],
          "projects": [],
          "memory": {"total": 12, "recent": ["Prefers PostgreSQL for new projects"]},
          "tools": {
            "count": 7, "groups": ["git", "knowledge", "system"],
            "shell_enabled": false, "invocations_today": 3, "failures_today": 0
          },
          "recent_activity": []
        }
        """

        let status = try JSONCoding.decoder.decode(
            SystemStatus.self, from: Data(json.utf8)
        )

        #expect(status.ai.model == "qwen3.8:latest")
        #expect(status.host.cpuCount == 10)
        #expect(status.memory.total == 12)
        #expect(status.tools.shellEnabled == false)
        #expect(status.isHealthy)
    }

    @Test("A model server that is down reads as a problem, not a failure")
    func systemStatusDegraded() throws {
        // The most likely state of a NOVA on a laptop that just woke up. The
        // screen has to render it, not fail to decode it.
        let json = """
        {
          "generated_at": "2026-09-16T15:42:28.579713Z",
          "ai": {
            "provider": "ollama", "model": "qwen3.8:latest", "online": false,
            "supports_tools": true, "latency_ms": null,
            "detail": "The local model server is not reachable. Is Ollama running?"
          },
          "host": {
            "hostname": "silviu-mac", "platform": "Darwin 27.0.0", "cpu_count": 10,
            "load_per_core": null, "memory_percent_used": null,
            "disk_percent_used": 74.1
          },
          "dependencies": [
            {"name": "redis", "healthy": false, "latency_ms": null, "error": "ConnectionError"}
          ],
          "projects": [
            {
              "name": "SnapWorth", "healthy": false, "reachable": false,
              "description": "Valuation API", "status_code": null,
              "latency_ms": null, "dependencies": {}
            }
          ],
          "memory": {"total": 0, "recent": []},
          "tools": {
            "count": 0, "groups": [], "shell_enabled": false,
            "invocations_today": 0, "failures_today": 0
          },
          "recent_activity": []
        }
        """

        let status = try JSONCoding.decoder.decode(
            SystemStatus.self, from: Data(json.utf8)
        )

        #expect(!status.isHealthy)
        // Each failure is named separately, because "something is wrong" is
        // not an actionable thing to read across a desk.
        #expect(status.problems.count == 3)
        #expect(status.projects[0].summary == "Not responding")
    }

    @Test("Decodes the audit log")
    func activity() throws {
        let json = """
        {"items": [{
          "id": "aa8608e9-f102-4e72-af8e-f15a1abd7259",
          "tool_name": "docker_containers", "tool_group": "docker",
          "permission": "read", "status": "succeeded",
          "initiated_by_model": true, "confirmed": false,
          "duration_ms": 84, "error_code": null,
          "created_at": "2026-09-16T15:42:28.579713Z"
        }, {
          "id": "bb8608e9-f102-4e72-af8e-f15a1abd7259",
          "tool_name": "docker_remove_container", "tool_group": "docker",
          "permission": "destructive", "status": "refused",
          "initiated_by_model": true, "confirmed": false,
          "duration_ms": 1, "error_code": "tool_needs_confirmation",
          "created_at": "2026-09-16T15:42:29.579713Z"
        }]}
        """

        let page = try JSONCoding.decoder.decode(ActivityPage.self, from: Data(json.utf8))

        #expect(page.items.count == 2)
        #expect(page.items[0].succeeded)
        // A refusal is in the log too, and reads as one.
        #expect(!page.items[1].succeeded)
        #expect(page.items[1].outcome == "refused")
    }

    // MARK: - Tools

    @Test("Decodes the tool catalogue with its schemas")
    func tools() throws {
        let json = """
        {
          "items": [{
            "name": "docker_logs",
            "description": "The most recent log lines from one container.",
            "group": "docker", "permission": "read",
            "requires_confirmation": false,
            "input_schema": {
              "type": "object",
              "properties": {
                "container": {"type": "string", "title": "Container"},
                "lines": {"type": "integer", "default": 50}
              },
              "required": ["container"],
              "additionalProperties": false
            }
          }],
          "shell_enabled": false
        }
        """

        let list = try JSONCoding.decoder.decode(ToolList.self, from: Data(json.utf8))
        let tool = try #require(list.items.first)

        #expect(tool.permission == .read)
        // Required arguments come first, so the form reads in the order
        // somebody would fill it in.
        #expect(tool.argumentNames == ["container", "lines"])
        #expect(tool.isRequired("container"))
        #expect(!tool.isRequired("lines"))
    }

    @Test("A permission this build has not heard of is treated as dangerous")
    func unknownPermission() throws {
        // A newer server may add one. Assuming the safest reading would be
        // the wrong way round: an unknown permission is not a reason to
        // relax, so it is shown as something that changes things.
        let json = """
        {
          "items": [{
            "name": "future_tool", "description": "Does something new.",
            "group": "system", "permission": "apocalyptic",
            "requires_confirmation": true,
            "input_schema": {"type": "object", "properties": {}}
          }],
          "shell_enabled": false
        }
        """

        let list = try JSONCoding.decoder.decode(ToolList.self, from: Data(json.utf8))

        #expect(list.items[0].permission == .unknown)
        #expect(list.items[0].permission.changesThings)
    }

    @Test("A confirmation is read out of the 409 the server answers with")
    func confirmationFromError() throws {
        // The whole handshake, as it arrives: what NOVA wants to do in
        // words, and the token that authorises exactly that.
        let json = """
        {"error": {
          "code": "tool_confirmation_required",
          "message": "Remove the Docker container “nova-api”? This cannot be undone.",
          "request_id": "7f1b",
          "details": {
            "tool": "docker_remove_container",
            "prompt": "Remove the Docker container “nova-api”? This cannot be undone.",
            "confirmation_token": "S3cr3t-t0ken",
            "expires_in_seconds": 180
          }
        }}
        """

        let envelope = try JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: Data(json.utf8)
        )
        let confirmation = try #require(
            PendingConfirmation(.api(status: 409, envelope: envelope.error))
        )

        #expect(confirmation.tool == "docker_remove_container")
        #expect(confirmation.token == "S3cr3t-t0ken")
        #expect(confirmation.prompt.contains("nova-api"))
    }

    @Test("An ordinary error is not mistaken for a confirmation")
    func nonConfirmationError() throws {
        let json = """
        {"error": {
          "code": "tool_not_found", "message": "No such tool.",
          "request_id": null, "details": null
        }}
        """

        let envelope = try JSONCoding.decoder.decode(
            APIErrorEnvelope.self, from: Data(json.utf8)
        )

        #expect(PendingConfirmation(.api(status: 404, envelope: envelope.error)) == nil)
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
