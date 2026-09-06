"""Password hashing and token issuance."""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
import pytest

from nova.core.config import JWTSettings, SecuritySettings
from nova.core.errors import AuthenticationError
from nova.core.security import Argon2PasswordHasher, TokenService

SECRET = "unit-test-secret-key-at-least-thirty-two-chars"


@pytest.fixture
def hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher(
        SecuritySettings(argon2_time_cost=1, argon2_memory_cost_kib=8, argon2_parallelism=1)
    )


@pytest.fixture
def tokens() -> TokenService:
    return TokenService(JWTSettings(secret_key=SECRET))  # type: ignore[arg-type]


class TestPasswordHashing:
    def test_hash_verifies_against_original(self, hasher: Argon2PasswordHasher) -> None:
        digest = hasher.hash("correct-horse-battery-staple")
        assert hasher.verify("correct-horse-battery-staple", digest)

    def test_hash_rejects_wrong_password(self, hasher: Argon2PasswordHasher) -> None:
        digest = hasher.hash("correct-horse-battery-staple")
        assert not hasher.verify("wrong-password", digest)

    def test_hash_is_salted(self, hasher: Argon2PasswordHasher) -> None:
        # Two hashes of the same password must differ, or the store is
        # vulnerable to rainbow tables.
        assert hasher.hash("same-password") != hasher.hash("same-password")

    def test_hash_is_argon2id(self, hasher: Argon2PasswordHasher) -> None:
        assert hasher.hash("whatever").startswith("$argon2id$")

    def test_malformed_hash_does_not_raise(self, hasher: Argon2PasswordHasher) -> None:
        # A corrupted row must read as "not authenticated", never a 500.
        assert not hasher.verify("password", "not-a-valid-argon2-hash")

    def test_needs_rehash_on_weaker_factors(self) -> None:
        weak = Argon2PasswordHasher(SecuritySettings(argon2_time_cost=1, argon2_memory_cost_kib=8))
        strong = Argon2PasswordHasher(
            SecuritySettings(argon2_time_cost=3, argon2_memory_cost_kib=16)
        )
        assert strong.needs_rehash(weak.hash("password"))

    def test_needs_rehash_false_for_current_factors(self, hasher: Argon2PasswordHasher) -> None:
        assert not hasher.needs_rehash(hasher.hash("password"))


class TestAccessTokens:
    def test_roundtrip_preserves_subject(self, tokens: TokenService) -> None:
        subject = uuid.uuid4()
        issued = tokens.issue_access_token(subject)
        assert tokens.decode_access_token(issued.token).subject == subject

    def test_rejects_wrong_signing_key(self, tokens: TokenService) -> None:
        issued = tokens.issue_access_token(uuid.uuid4())
        attacker = TokenService(
            JWTSettings(secret_key="a-completely-different-secret-key-value")  # type: ignore[arg-type]
        )
        with pytest.raises(AuthenticationError):
            attacker.decode_access_token(issued.token)

    def test_rejects_alg_none_confusion(self, tokens: TokenService) -> None:
        # The classic JWT attack: re-sign with "none" and hope the verifier
        # honours the token's own alg header.
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "jti": "forged",
                "typ": "access",
                "iat": 0,
                "exp": 9_999_999_999,
                "iss": "nova-api",
                "aud": "nova-clients",
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthenticationError):
            tokens.decode_access_token(forged)

    def test_rejects_expired_token(self) -> None:
        service = TokenService(
            JWTSettings(secret_key=SECRET, access_token_ttl_seconds=60)  # type: ignore[arg-type]
        )
        issued = service.issue_access_token(uuid.uuid4())
        # Re-sign the same claims into the past rather than sleeping.
        payload = jwt.decode(
            issued.token,
            SECRET,
            algorithms=["HS256"],
            issuer="nova-api",
            audience="nova-clients",
        )
        payload["exp"] = payload["iat"] - 1
        expired = jwt.encode(payload, SECRET, algorithm="HS256")
        with pytest.raises(AuthenticationError) as exc:
            service.decode_access_token(expired)
        assert exc.value.code == "token_expired"

    def test_rejects_wrong_audience(self, tokens: TokenService) -> None:
        other = TokenService(
            JWTSettings(secret_key=SECRET, audience="someone-else")  # type: ignore[arg-type]
        )
        with pytest.raises(AuthenticationError):
            tokens.decode_access_token(other.issue_access_token(uuid.uuid4()).token)

    def test_rejects_wrong_issuer(self, tokens: TokenService) -> None:
        other = TokenService(
            JWTSettings(secret_key=SECRET, issuer="not-nova")  # type: ignore[arg-type]
        )
        with pytest.raises(AuthenticationError):
            tokens.decode_access_token(other.issue_access_token(uuid.uuid4()).token)

    def test_rejects_garbage(self, tokens: TokenService) -> None:
        with pytest.raises(AuthenticationError):
            tokens.decode_access_token("not.a.jwt")

    def test_expiry_matches_configured_ttl(self) -> None:
        service = TokenService(
            JWTSettings(secret_key=SECRET, access_token_ttl_seconds=900)  # type: ignore[arg-type]
        )
        issued = service.issue_access_token(uuid.uuid4())
        claims = service.decode_access_token(issued.token)
        assert claims.expires_at - claims.issued_at == timedelta(seconds=900)

    def test_each_token_has_a_unique_jti(self, tokens: TokenService) -> None:
        subject = uuid.uuid4()
        first = tokens.issue_access_token(subject)
        second = tokens.issue_access_token(subject)
        assert first.jti != second.jti


class TestRefreshTokens:
    def test_plaintext_is_not_the_stored_digest(self, tokens: TokenService) -> None:
        material = tokens.issue_refresh_token()
        assert material.plaintext != material.digest

    def test_digest_is_deterministic(self, tokens: TokenService) -> None:
        material = tokens.issue_refresh_token()
        assert tokens.hash_refresh_token(material.plaintext) == material.digest

    def test_digest_is_sha256_hex(self, tokens: TokenService) -> None:
        # The column is String(64); a longer digest would silently truncate.
        assert len(tokens.issue_refresh_token().digest) == 64

    def test_tokens_are_unique(self, tokens: TokenService) -> None:
        minted = {tokens.issue_refresh_token().plaintext for _ in range(50)}
        assert len(minted) == 50


class TestJWTSettingsValidation:
    def test_rejects_short_secret(self) -> None:
        with pytest.raises(ValueError, match="at least 32 characters"):
            JWTSettings(secret_key="too-short")  # type: ignore[arg-type]
