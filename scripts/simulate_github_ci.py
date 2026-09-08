"""Pretend to be GitHub: sign a workflow_run delivery and post it to NOVA.

    python scripts/simulate_github_ci.py --secret <secret> --url <webhook_url> [--fail]

The secret and URL are what `POST /api/v1/integrations/github` returned.
Run `scripts/simulate_device.py` first, or have a real device connected, and
watch it react: a green build is a happy face and "Build's green."; a red one
is a confused face and "Build failed."

This signs the body exactly as GitHub does -- HMAC-SHA256 over the raw bytes,
in the X-Hub-Signature-256 header -- so it exercises the same verification a
real delivery goes through. It cannot be used to forge a delivery without
the secret, which is the point of the secret.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import uuid

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="the integration's webhook_url")
    parser.add_argument("--secret", required=True, help="the secret shown once at creation")
    parser.add_argument("--repository", default="hsilviu05/nova")
    parser.add_argument("--fail", action="store_true", help="deliver a failed run instead")
    parser.add_argument("--merged", action="store_true", help="deliver a merged pull request")
    args = parser.parse_args(argv)

    if args.merged:
        event = "pull_request"
        payload = {
            "action": "closed",
            "pull_request": {"merged": True, "number": 1, "title": "Something"},
            "repository": {"full_name": args.repository},
        }
    else:
        event = "workflow_run"
        payload = {
            "action": "completed",
            "workflow_run": {"conclusion": "failure" if args.fail else "success", "name": "CI"},
            "repository": {"full_name": args.repository},
        }

    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": signature,
        "User-Agent": "GitHub-Hookshot/simulated",
    }

    try:
        response = httpx.post(args.url, content=body, headers=headers, timeout=10)
    except httpx.HTTPError as error:
        print(f"delivery failed: {error}", file=sys.stderr)
        return 1

    print(f"{event} -> HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
