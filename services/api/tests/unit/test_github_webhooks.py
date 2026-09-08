"""The pure half of GitHub dev mode.

Verification is the part that guards a public URL, so the cases are the
ways a forged or tampered delivery could get through.
"""

from __future__ import annotations

import json

import pytest

from nova.services.github_webhooks import (
    Ignored,
    Reaction,
    react,
    repository_full_name,
    sign,
    verify_signature,
)

SECRET = "a-webhook-secret-of-reasonable-length"
BODY = json.dumps({"action": "completed", "workflow_run": {"conclusion": "success"}}).encode()


class TestSignature:
    def test_a_signature_over_the_same_body_verifies(self) -> None:
        assert verify_signature(SECRET, BODY, sign(SECRET, BODY))

    def test_the_header_has_githubs_prefix(self) -> None:
        assert sign(SECRET, BODY).startswith("sha256=")

    def test_a_tampered_body_fails(self) -> None:
        header = sign(SECRET, BODY)
        assert not verify_signature(SECRET, BODY + b" ", header)
        assert not verify_signature(SECRET, BODY.replace(b"success", b"failure"), header)

    def test_the_wrong_secret_fails(self) -> None:
        assert not verify_signature("another-secret", BODY, sign(SECRET, BODY))

    def test_a_missing_or_malformed_header_fails(self) -> None:
        assert not verify_signature(SECRET, BODY, None)
        assert not verify_signature(SECRET, BODY, "")
        assert not verify_signature(SECRET, BODY, sign(SECRET, BODY).removeprefix("sha256="))
        assert not verify_signature(SECRET, BODY, "sha1=deadbeef")

    def test_a_signature_of_a_different_length_does_not_raise(self) -> None:
        # compare_digest on unequal lengths returns False; it must not throw
        # on the one input an attacker fully controls.
        assert not verify_signature(SECRET, BODY, "sha256=abc")


class TestReactionTable:
    def test_green_build(self) -> None:
        result = react(
            "workflow_run", {"action": "completed", "workflow_run": {"conclusion": "success"}}
        )
        assert isinstance(result, Reaction)
        assert result.kind == "ci_passed"
        assert result.emotion == "happy"
        assert result.summary == "workflow_run:success"

    def test_red_build_is_confused_not_alert(self) -> None:
        # A failed test is not an intruder; the face should say "hm", not
        # "who's there".
        result = react(
            "workflow_run", {"action": "completed", "workflow_run": {"conclusion": "failure"}}
        )
        assert isinstance(result, Reaction)
        assert result.kind == "ci_failed"
        assert result.emotion == "confused"

    @pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "timed_out", "neutral", None])
    def test_other_conclusions_are_ignored(self, conclusion: str | None) -> None:
        result = react(
            "workflow_run", {"action": "completed", "workflow_run": {"conclusion": conclusion}}
        )
        assert isinstance(result, Ignored)

    def test_a_run_that_has_not_completed_is_ignored(self) -> None:
        result = react(
            "workflow_run", {"action": "requested", "workflow_run": {"conclusion": None}}
        )
        assert isinstance(result, Ignored)

    def test_merged_pull_request(self) -> None:
        result = react("pull_request", {"action": "closed", "pull_request": {"merged": True}})
        assert isinstance(result, Reaction)
        assert result.kind == "pr_merged"

    def test_closed_without_merge_is_ignored(self) -> None:
        result = react("pull_request", {"action": "closed", "pull_request": {"merged": False}})
        assert isinstance(result, Ignored)

    def test_ping_and_unknown_events_are_ignored_not_errors(self) -> None:
        assert isinstance(react("ping", {"zen": "Keep it logically awesome."}), Ignored)
        assert isinstance(react("issues", {"action": "opened"}), Ignored)
        assert isinstance(react(None, {}), Ignored)

    def test_a_malformed_payload_is_ignored_not_an_exception(self) -> None:
        # GitHub retries anything that is not 2xx, so a strange shape must
        # be swallowed rather than turned into a 500 that comes back forever.
        assert isinstance(
            react("workflow_run", {"action": "completed", "workflow_run": "nope"}), Ignored
        )
        assert isinstance(react("pull_request", {"action": "closed"}), Ignored)
        assert isinstance(react("workflow_run", {}), Ignored)

    def test_lines_are_short(self) -> None:
        # A creature, not a notification.
        for event, payload in (
            ("workflow_run", {"action": "completed", "workflow_run": {"conclusion": "success"}}),
            ("workflow_run", {"action": "completed", "workflow_run": {"conclusion": "failure"}}),
            ("pull_request", {"action": "closed", "pull_request": {"merged": True}}),
        ):
            result = react(event, payload)
            assert isinstance(result, Reaction)
            assert len(result.line.split()) <= 4


class TestRepositoryName:
    def test_reads_full_name(self) -> None:
        assert (
            repository_full_name({"repository": {"full_name": "hsilviu05/nova"}})
            == "hsilviu05/nova"
        )

    def test_absent_or_malformed_is_none(self) -> None:
        assert repository_full_name({}) is None
        assert repository_full_name({"repository": "nova"}) is None
        assert repository_full_name({"repository": {"full_name": ""}}) is None
