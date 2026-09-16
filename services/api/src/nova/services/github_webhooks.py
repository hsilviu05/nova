"""The pure half of GitHub dev mode: verifying a delivery, and deciding
what NOVA does about it.

No database, no Redis, no sockets. Two functions and a table, all of which
a test can call with a byte string and a dict -- which matters more than
usual here, because the endpoint they protect is the one unauthenticated
public URL in the whole API.

**Verification is over the raw body.** GitHub signs the bytes it sent. The
JSON is parsed only after the signature passes; parsing first would mean
running a decoder on attacker-controlled input to decide whether it came
from an attacker.

**The mapping is a table, not a model.** The language model never reaches
GPIO, and it does not reach this either: a green build is a fixed emotion
and a fixed sentence. It is deterministic, it is testable, and it cannot be
prompt-injected by a commit message.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Literal

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

# GitHub payloads for these events are tens of kilobytes. Anything in the
# megabytes is not GitHub, and reading it would be the attacker's choice of
# memory allocation on our side.
MAX_BODY_BYTES = 512 * 1024

_PREFIX = "sha256="


def sign(secret: str, body: bytes) -> str:
    """The header value GitHub would send for ``body``. Used by the tests
    and the simulator; the server only ever compares."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` against ``body``.

    ``compare_digest`` rather than ``==``: a byte-by-byte comparison leaks
    how many leading bytes matched through its timing, and this is the one
    place in the API where an attacker can make unlimited guesses.
    """
    if not header or not header.startswith(_PREFIX):
        return False
    expected = sign(secret, body)
    return hmac.compare_digest(expected.encode("utf-8"), header.encode("utf-8"))


ReactionKind = Literal["ci_passed", "ci_failed", "pr_merged"]

# The face vocabulary the protocol accepts; kept to the subset that reads
# right on a desk. "alert" would be wrong for a red build -- a failed test
# is not an intruder.
Emotion = Literal["happy", "confused", "curious"]


@dataclass(frozen=True, slots=True)
class Reaction:
    kind: ReactionKind
    emotion: Emotion
    line: str
    # For the integration's last_event field and the logs.
    summary: str


@dataclass(frozen=True, slots=True)
class Ignored:
    reason: str


# Fixed, deliberately plain. A desk companion that delivers a paragraph
# about your build is a notification, not a creature.
_LINES = {
    "ci_passed": "Build's green.",
    "ci_failed": "Build failed.",
    "pr_merged": "Merged.",
}


def react(event: str | None, payload: dict[str, Any]) -> Reaction | Ignored:
    """Decide what a delivery means. Never raises on a strange payload --
    a shape this code does not know is ignored, not an error, because
    GitHub retries anything that is not a 2xx."""
    if event == "ping":
        return Ignored("ping")

    if event == "workflow_run":
        if payload.get("action") != "completed":
            return Ignored("workflow_run not completed")
        run = payload.get("workflow_run")
        conclusion = run.get("conclusion") if isinstance(run, dict) else None
        if conclusion == "success":
            return Reaction("ci_passed", "happy", _LINES["ci_passed"], "workflow_run:success")
        if conclusion == "failure":
            return Reaction("ci_failed", "confused", _LINES["ci_failed"], "workflow_run:failure")
        # cancelled, skipped, timed_out, neutral, stale, action_required: none
        # of these is news worth a face.
        return Ignored(f"workflow_run conclusion {conclusion!r}")

    if event == "pull_request":
        pull = payload.get("pull_request")
        merged = pull.get("merged") if isinstance(pull, dict) else None
        if payload.get("action") == "closed" and merged is True:
            return Reaction("pr_merged", "happy", _LINES["pr_merged"], "pull_request:merged")
        return Ignored("pull_request not a merge")

    return Ignored(f"event {event!r} not handled")


def repository_full_name(payload: dict[str, Any]) -> str | None:
    """``owner/name`` from any GitHub payload that carries a repository."""
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    name = repository.get("full_name")
    return name if isinstance(name, str) and name else None
