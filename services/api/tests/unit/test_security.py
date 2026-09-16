"""Password hashing and token issuance."""

from __future__ import annotations

import time
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


ISSUER = "nova-api"
AUDIENCE = "nova-clients"


class TestHashesThatAreNotHashes:
    """A stored hash can be wrong in ways a password never is.

    A hand-edited row, a restored backup from a different argon2 build, a
    column truncated by a migration. None of those should look different to
    an attacker from a wrong password, and none should crash the login route.
    """

    @pytest.mark.parametrize(
        "stored", ["not-a-hash", "", "$argon2id$v=19$m=65536", "plaintext-password"]
    )
    def test_a_malformed_stored_hash_reads_as_a_failed_login(
        self, hasher: Argon2PasswordHasher, stored: str
    ) -> None:
        assert hasher.verify("correct horse battery staple", stored) is False

    def test_a_hash_that_cannot_be_read_is_treated_as_needing_a_rehash(
        self, hasher: Argon2PasswordHasher
    ) -> None:
        """Fails towards re-hashing.

        The alternative -- reporting "this is current" for a hash that cannot
        be parsed -- would leave a bad row in place forever.
        """
        assert hasher.needs_rehash("not-a-hash") is True

    def test_a_current_hash_does_not_need_rehashing(self, hasher: Argon2PasswordHasher) -> None:
        assert hasher.needs_rehash(hasher.hash("correct horse battery staple")) is False


class TestTokensWithMissingClaims:
    def test_a_token_whose_subject_is_not_a_uuid_is_refused(self, tokens: TokenService) -> None:
        """The subject goes straight into a database lookup.

        Anything that is not a UUID is refused here rather than allowed to
        become a query with a string where an id belongs.
        """
        forged = jwt.encode(
            {
                "sub": "not-a-uuid",
                "jti": "abc",
                "typ": "access",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "iss": ISSUER,
                "aud": AUDIENCE,
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(AuthenticationError) as caught:
            tokens.decode_access_token(forged)

        assert caught.value.code == "token_invalid"

    def test_a_token_with_no_subject_at_all_is_refused(self, tokens: TokenService) -> None:
        """``require`` in the decode options should catch this first. Tested
        anyway, because the two checks guard different failures and one of
        them could be relaxed without the other being noticed."""
        forged = jwt.encode(
            {
                "jti": "abc",
                "typ": "access",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "iss": ISSUER,
                "aud": AUDIENCE,
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(AuthenticationError) as caught:
            tokens.decode_access_token(forged)

        assert caught.value.code == "token_invalid"

    @pytest.mark.parametrize("kind", ["refresh", "reset", "", None])
    def test_a_token_of_any_other_class_is_not_an_access_token(
        self, tokens: TokenService, kind: str | None
    ) -> None:
        """A correctly signed token is not automatically an access token.

        Without this check, any other token class NOVA ever signs with the
        same key -- today or later -- would be accepted at the API's front
        door. The signature is valid; the claim is what makes it an access
        token.
        """
        claims = {
            "sub": str(uuid.uuid4()),
            "jti": "abc",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "iss": ISSUER,
            "aud": AUDIENCE,
        }
        if kind is not None:
            claims["typ"] = kind

        forged = jwt.encode(claims, SECRET, algorithm="HS256")

        with pytest.raises(AuthenticationError) as caught:
            tokens.decode_access_token(forged)

        assert caught.value.code == "token_invalid"
