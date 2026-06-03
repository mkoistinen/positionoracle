"""Tests for API key generation, hashing, and verification."""

from __future__ import annotations

import pytest

from positionoracle.api_keys import (
    _ENTROPY_BYTES,
    _KEY_PREFIX_TAG,
    _PREFIX_DISPLAY_LEN,
    generate_key,
    hash_key,
    verify_key,
)


class TestGenerateKey:
    def test_returns_three_values(self):
        cleartext, digest, prefix = generate_key()
        assert cleartext and digest and prefix

    def test_cleartext_has_po_prefix(self):
        cleartext, _, _ = generate_key()
        assert cleartext.startswith(_KEY_PREFIX_TAG)

    def test_display_prefix_length(self):
        _, _, prefix = generate_key()
        assert len(prefix) == _PREFIX_DISPLAY_LEN

    def test_display_prefix_matches_cleartext(self):
        cleartext, _, prefix = generate_key()
        assert cleartext[:_PREFIX_DISPLAY_LEN] == prefix

    def test_digest_is_sha256_hex(self):
        _, digest, _ = generate_key()
        # SHA-256 hex digest is 64 chars of 0-9a-f.
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_unique_each_call(self):
        a, _, _ = generate_key()
        b, _, _ = generate_key()
        assert a != b

    def test_cleartext_has_enough_entropy(self):
        cleartext, _, _ = generate_key()
        # Strip "po_" and check base64 length matches 32 bytes (>= 42 chars).
        token = cleartext[len(_KEY_PREFIX_TAG):]
        # base64.urlsafe_b64encode of 32 bytes is ~43 chars without padding.
        assert len(token) >= _ENTROPY_BYTES


class TestVerifyKey:
    def test_round_trip_succeeds(self):
        cleartext, digest, _ = generate_key()
        assert verify_key(cleartext, digest) is True

    def test_mismatch_fails(self):
        cleartext, digest, _ = generate_key()
        assert verify_key(cleartext + "x", digest) is False

    def test_empty_string_does_not_match(self):
        _, digest, _ = generate_key()
        assert verify_key("", digest) is False

    def test_different_key_does_not_match(self):
        cleartext_a, _, _ = generate_key()
        _, digest_b, _ = generate_key()
        assert verify_key(cleartext_a, digest_b) is False


class TestHashKey:
    def test_deterministic(self):
        assert hash_key("po_test") == hash_key("po_test")

    @pytest.mark.parametrize("a,b", [
        ("po_a", "po_b"),
        ("po_", ""),
        ("po_aaa", "po_aab"),
    ])
    def test_distinct_inputs_distinct_hashes(self, a, b):
        assert hash_key(a) != hash_key(b)
