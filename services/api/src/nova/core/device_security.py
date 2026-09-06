"""Secrets used by the device claim flow.

Three distinct values, each sized for how it is used:

* **Claim code** -- shown on the device's screen and typed by a person, so it
  is short by necessity. Compensated for elsewhere: short TTL, single use,
  attempt cap, rate limiting.
* **Provisioning token** -- 256 bits, never displayed. Proves a request comes
  from the device that started provisioning.
* **Device token** -- 384 bits, the long-lived credential. Prefixed so that
  secret scanners recognise it if it ever leaks into a repository or a log.

All three are stored as SHA-256 digests. As with refresh tokens, the inputs
are full-entropy random data rather than guessable passwords, so there is
nothing for a slow KDF to defend against.

The claim code is the exception worth naming: at roughly 29 bits it *is*
brute-forceable offline if the database leaks. Hashing it is still correct,
but the real defences are its ten-minute life and its single use.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from nova.core.clock import utc_now

# Ambiguous glyphs removed so a code read off a small screen transcribes
# cleanly: no I/1, no O/0, no L, no U/V confusion.
CLAIM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ23456789"
CLAIM_CODE_LENGTH = 6
CLAIM_CODE_GROUP = 3

_PROVISIONING_TOKEN_BYTES = 32
_DEVICE_TOKEN_BYTES = 48

# Identifies a NOVA device credential on sight, and lets secret-scanning
# tooling match it. S105 is a false positive: this is the public prefix that
# marks a secret, not a secret itself.
DEVICE_TOKEN_PREFIX = "novad_"  # noqa: S105


@dataclass(frozen=True, slots=True)
class ClaimSecrets:
    """Everything minted when a device starts provisioning.

    ``code`` is displayed on the device and never stored in plaintext.
    ``provisioning_token`` is returned to the device once and never displayed.
    """

    code: str
    code_digest: str
    provisioning_token: str
    provisioning_token_digest: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceTokenMaterial:
    """A freshly minted device credential."""

    plaintext: str
    digest: str


def generate_claim_code() -> str:
    """Return a human-transcribable claim code, e.g. ``K7M-P29``.

    ``secrets.choice`` is uniform over the alphabet regardless of its length,
    so the non-power-of-two alphabet introduces no modulo bias.
    """
    raw = "".join(secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(CLAIM_CODE_LENGTH))
    return f"{raw[:CLAIM_CODE_GROUP]}-{raw[CLAIM_CODE_GROUP:]}"


def normalise_claim_code(code: str) -> str:
    """Canonicalise user input before lookup.

    People type lower case, omit the hyphen, or paste surrounding spaces.
    All of those should find the same code.
    """
    return "".join(code.split()).replace("-", "").upper()


def hash_claim_code(code: str) -> str:
    """Digest a claim code for storage and lookup, normalising first."""
    return _sha256(normalise_claim_code(code))


def generate_provisioning_token() -> tuple[str, str]:
    """Return a provisioning token and its digest."""
    token = secrets.token_urlsafe(_PROVISIONING_TOKEN_BYTES)
    return token, _sha256(token)


def hash_provisioning_token(token: str) -> str:
    return _sha256(token)


def issue_claim_secrets(*, ttl_seconds: int) -> ClaimSecrets:
    """Mint the code and token pair for one provisioning attempt."""
    code = generate_claim_code()
    provisioning_token, provisioning_digest = generate_provisioning_token()
    return ClaimSecrets(
        code=code,
        code_digest=hash_claim_code(code),
        provisioning_token=provisioning_token,
        provisioning_token_digest=provisioning_digest,
        expires_at=utc_now() + timedelta(seconds=ttl_seconds),
    )


def issue_device_token() -> DeviceTokenMaterial:
    """Mint a long-lived device credential."""
    plaintext = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(_DEVICE_TOKEN_BYTES)
    return DeviceTokenMaterial(plaintext=plaintext, digest=hash_device_token(plaintext))


def hash_device_token(token: str) -> str:
    """Digest a device token for storage and lookup."""
    return _sha256(token)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
