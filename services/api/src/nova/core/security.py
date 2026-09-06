"""Password hashing and token issuance.

Two different token types, deliberately:

* **Access token** -- a short-lived signed JWT. Stateless, so every request
  verifies it without touching the database.
* **Refresh token** -- an opaque 384-bit random string, stored only as a
  SHA-256 digest. Opaque because a refresh token must be *revocable*, and a
  stateless JWT cannot be revoked before it expires.

The refresh digest uses plain SHA-256 rather than Argon2 on purpose: the input
is full-entropy random data, not a guessable human password, so there is
nothing for a slow KDF to defend against and per-request hashing stays cheap.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt
from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from nova.core.clock import utc_now
from nova.core.config import JWTSettings, SecuritySettings
from nova.core.errors import AuthenticationError

# Number of random bytes behind a refresh token (48 bytes -> 384 bits).
_REFRESH_TOKEN_BYTES = 48


class PasswordHasher(Protocol):
    """Hashing strategy. Services depend on this, not on argon2 directly."""

    def hash(self, password: str) -> str: ...

    def verify(self, password: str, password_hash: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...


class Argon2PasswordHasher:
    """Argon2id password hashing.

    Argon2id is the OWASP first-choice KDF: memory-hard, so GPU and ASIC
    cracking gains far less than it does against SHA-family hashes.
    """

    def __init__(self, settings: SecuritySettings) -> None:
        self._hasher = Argon2Hasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            # A malformed stored hash is indistinguishable from a wrong
            # password to the caller; both are simply "not authenticated".
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        """True when the stored hash predates the current work factors."""
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True


@dataclass(frozen=True, slots=True)
class AccessToken:
    """An issued access token and the moment it stops being valid."""

    token: str
    expires_at: datetime
    jti: str


@dataclass(frozen=True, slots=True)
class RefreshTokenMaterial:
    """A freshly minted refresh token.

    ``plaintext`` is returned to the client exactly once. Only ``digest``
    is persisted, so a database leak does not yield usable tokens.
    """

    plaintext: str
    digest: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Verified claims from an access token."""

    subject: uuid.UUID
    jti: str
    issued_at: datetime
    expires_at: datetime


class TokenService:
    """Issues and verifies authentication tokens."""

    def __init__(self, settings: JWTSettings) -> None:
        self._settings = settings

    # -- access tokens ----------------------------------------------------

    def issue_access_token(self, subject: uuid.UUID) -> AccessToken:
        """Sign a short-lived access token for ``subject``."""
        now = utc_now()
        expires_at = now + timedelta(seconds=self._settings.access_token_ttl_seconds)
        jti = uuid.uuid4().hex

        payload: dict[str, Any] = {
            "sub": str(subject),
            "jti": jti,
            "typ": "access",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "iss": self._settings.issuer,
            "aud": self._settings.audience,
        }
        token = jwt.encode(
            payload,
            self._settings.secret_key.get_secret_value(),
            algorithm=self._settings.algorithm,
        )
        return AccessToken(token=token, expires_at=expires_at, jti=jti)

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        """Verify ``token`` and return its claims.

        Raises:
            AuthenticationError: if the signature, algorithm, issuer,
                audience, expiry, token type, or subject is not valid.
        """
        try:
            payload = jwt.decode(
                token,
                self._settings.secret_key.get_secret_value(),
                # Pin the algorithm. Accepting the token's own ``alg`` header
                # is the classic JWT confusion vulnerability.
                algorithms=[self._settings.algorithm],
                issuer=self._settings.issuer,
                audience=self._settings.audience,
                options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Access token has expired.", code="token_expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError("Access token is invalid.", code="token_invalid") from exc

        if payload.get("typ") != "access":
            # Refuse to accept any other token class here.
            raise AuthenticationError("Access token is invalid.", code="token_invalid")

        try:
            subject = uuid.UUID(str(payload["sub"]))
        except (KeyError, ValueError) as exc:
            raise AuthenticationError("Access token is invalid.", code="token_invalid") from exc

        return AccessTokenClaims(
            subject=subject,
            jti=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )

    # -- refresh tokens ---------------------------------------------------

    def issue_refresh_token(self) -> RefreshTokenMaterial:
        """Mint an opaque refresh token plus the digest to persist."""
        plaintext = secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)
        expires_at = utc_now() + timedelta(seconds=self._settings.refresh_token_ttl_seconds)
        return RefreshTokenMaterial(
            plaintext=plaintext,
            digest=self.hash_refresh_token(plaintext),
            expires_at=expires_at,
        )

    @staticmethod
    def hash_refresh_token(plaintext: str) -> str:
        """Digest a refresh token for storage and lookup."""
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
