"""Claim codes, provisioning tokens, and device credentials."""

from __future__ import annotations

import pytest

from nova.core.device_security import (
    CLAIM_CODE_ALPHABET,
    DEVICE_TOKEN_PREFIX,
    generate_claim_code,
    hash_claim_code,
    hash_device_token,
    issue_claim_secrets,
    issue_device_token,
    normalise_claim_code,
)


class TestClaimCodes:
    def test_is_formatted_in_two_groups(self) -> None:
        code = generate_claim_code()
        assert len(code) == 7
        assert code[3] == "-"

    def test_uses_only_unambiguous_glyphs(self) -> None:
        """A code is read off a small screen and typed by hand."""
        for _ in range(200):
            assert set(generate_claim_code().replace("-", "")) <= set(CLAIM_CODE_ALPHABET)

    @pytest.mark.parametrize("glyph", "ILOU01")
    def test_excludes_confusable_glyphs(self, glyph: str) -> None:
        assert glyph not in CLAIM_CODE_ALPHABET

    def test_codes_vary(self) -> None:
        assert len({generate_claim_code() for _ in range(200)}) > 190

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("K7M-P29", "K7MP29"),
            ("k7m-p29", "K7MP29"),
            ("K7MP29", "K7MP29"),
            ("  K7M-P29  ", "K7MP29"),
            ("K7M P29", "K7MP29"),
        ],
    )
    def test_normalisation_is_forgiving(self, raw: str, expected: str) -> None:
        """People type lower case, drop the hyphen, and paste stray spaces."""
        assert normalise_claim_code(raw) == expected

    def test_hashing_follows_normalisation(self) -> None:
        assert hash_claim_code("K7M-P29") == hash_claim_code("k7mp29")

    def test_different_codes_hash_differently(self) -> None:
        assert hash_claim_code("AAA-AAA") != hash_claim_code("BBB-BBB")


class TestClaimSecrets:
    def test_mints_two_independent_secrets(self) -> None:
        secrets_ = issue_claim_secrets(ttl_seconds=600)

        # The screen code and the device's token must never coincide: the
        # whole design rests on knowing one not implying the other.
        assert secrets_.code != secrets_.provisioning_token
        assert secrets_.code_digest != secrets_.provisioning_token_digest

    def test_digests_match_their_plaintext(self) -> None:
        secrets_ = issue_claim_secrets(ttl_seconds=600)
        assert hash_claim_code(secrets_.code) == secrets_.code_digest

    def test_provisioning_token_is_long(self) -> None:
        # Never displayed, so it carries real entropy rather than being sized
        # for a human to retype.
        assert len(issue_claim_secrets(ttl_seconds=600).provisioning_token) > 40

    def test_expiry_honours_the_ttl(self) -> None:
        from nova.core.clock import utc_now

        secrets_ = issue_claim_secrets(ttl_seconds=600)
        remaining = (secrets_.expires_at - utc_now()).total_seconds()
        assert 590 < remaining <= 600


class TestDeviceTokens:
    def test_carries_a_recognisable_prefix(self) -> None:
        # So secret scanners can match it if it ever leaks.
        assert issue_device_token().plaintext.startswith(DEVICE_TOKEN_PREFIX)

    def test_digest_is_sha256_hex(self) -> None:
        # The column is String(64); anything longer would silently truncate.
        assert len(issue_device_token().digest) == 64

    def test_digest_matches_the_plaintext(self) -> None:
        token = issue_device_token()
        assert hash_device_token(token.plaintext) == token.digest

    def test_plaintext_is_not_the_digest(self) -> None:
        token = issue_device_token()
        assert token.plaintext != token.digest

    def test_tokens_are_unique(self) -> None:
        assert len({issue_device_token().plaintext for _ in range(100)}) == 100
