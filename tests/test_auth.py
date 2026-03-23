import contextlib
import json

import pytest

from positionoracle.auth import (
    _challenges,
    begin_authentication,
    begin_registration,
    complete_authentication,
    complete_registration,
    load_credentials,
    save_credentials,
)


class TestCredentialPersistence:
    def test_load_empty(self, data_dir):
        creds = load_credentials(data_dir)
        assert creds == []

    def test_save_and_load(self, data_dir):
        creds = [
            {
                "id": "test-id-1",
                "public_key": "test-pk-1",
                "sign_count": 0,
                "name": "Test Key",
                "registered_at": "2025-01-01T00:00:00",
            }
        ]
        save_credentials(data_dir, creds)
        loaded = load_credentials(data_dir)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "test-id-1"
        assert loaded[0]["name"] == "Test Key"

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "sub" / "dir"
        creds = [
            {
                "id": "test-id",
                "public_key": "test-pk",
                "sign_count": 0,
                "name": "Key",
                "registered_at": "2025-01-01T00:00:00",
            }
        ]
        save_credentials(nested, creds)
        assert (nested / "credentials.json").exists()

    def test_load_invalid_json(self, data_dir):
        path = data_dir / "credentials.json"
        path.write_text("not json", encoding="utf-8")
        creds = load_credentials(data_dir)
        assert creds == []

    def test_load_non_list_json(self, data_dir):
        path = data_dir / "credentials.json"
        path.write_text('{"not": "a list"}', encoding="utf-8")
        creds = load_credentials(data_dir)
        assert creds == []

    def test_multiple_credentials(self, data_dir):
        creds = [
            {
                "id": f"test-id-{i}",
                "public_key": f"test-pk-{i}",
                "sign_count": i,
                "name": f"Key {i}",
                "registered_at": "2025-01-01T00:00:00",
            }
            for i in range(3)
        ]
        save_credentials(data_dir, creds)
        loaded = load_credentials(data_dir)
        assert len(loaded) == 3


class TestRegistrationCeremony:
    def test_begin_registration_returns_options(self):
        options_json, token = begin_registration(
            rp_id="localhost",
            rp_name="Test",
            creds=[],
        )
        options = json.loads(options_json)
        assert "challenge" in options
        assert token in _challenges
        # Clean up
        _challenges.pop(token, None)

    def test_begin_registration_excludes_existing(self):
        creds = [
            {
                "id": "dGVzdC1pZA",
                "public_key": "dGVzdC1waw",
                "sign_count": 0,
                "name": "Existing",
                "registered_at": "2025-01-01T00:00:00",
            }
        ]
        options_json, token = begin_registration(
            rp_id="localhost",
            rp_name="Test",
            creds=creds,
        )
        options = json.loads(options_json)
        assert "excludeCredentials" in options
        _challenges.pop(token, None)


class TestAuthenticationCeremony:
    def test_begin_authentication_returns_options(self):
        creds = [
            {
                "id": "dGVzdC1pZA",
                "public_key": "dGVzdC1waw",
                "sign_count": 0,
                "name": "Key",
                "registered_at": "2025-01-01T00:00:00",
            }
        ]
        options_json, token = begin_authentication(
            rp_id="localhost",
            creds=creds,
        )
        options = json.loads(options_json)
        assert "challenge" in options
        assert token in _challenges
        _challenges.pop(token, None)

    def test_begin_authentication_empty_creds(self):
        options_json, token = begin_authentication(
            rp_id="localhost",
            creds=[],
        )
        options = json.loads(options_json)
        assert "challenge" in options
        _challenges.pop(token, None)


class TestCompleteRegistration:
    def test_invalid_challenge_token(self):
        with pytest.raises(ValueError, match="Invalid or expired"):
            complete_registration(
                credential_json="{}",
                challenge_token="nonexistent-token",
                rp_id="localhost",
                expected_origin="http://localhost:8000",
                name="Test",
            )

    def test_consumed_challenge_token(self):
        token = "test-token-consume"
        _challenges[token] = b"challenge-bytes"
        # First call consumes it (will fail at verification, but that's OK)
        with contextlib.suppress(Exception):
            complete_registration(
                credential_json="{}",
                challenge_token=token,
                rp_id="localhost",
                expected_origin="http://localhost:8000",
                name="Test",
            )
        # Second call should fail with "Invalid or expired"
        with pytest.raises(ValueError, match="Invalid or expired"):
            complete_registration(
                credential_json="{}",
                challenge_token=token,
                rp_id="localhost",
                expected_origin="http://localhost:8000",
                name="Test",
            )


class TestCompleteAuthentication:
    def test_invalid_challenge_token(self):
        result = complete_authentication(
            credential_json='{"id": "test", "rawId": "test"}',
            challenge_token="nonexistent-token",
            rp_id="localhost",
            expected_origin="http://localhost:8000",
            creds=[],
        )
        assert result is None

    def test_no_matching_credential(self):
        token = "test-auth-token"
        _challenges[token] = b"challenge-bytes"
        creds = [
            {
                "id": "different-id",
                "public_key": "dGVzdC1waw",
                "sign_count": 0,
                "name": "Key",
                "registered_at": "2025-01-01T00:00:00",
            }
        ]
        result = complete_authentication(
            credential_json={"id": "test-id", "rawId": "test-id"},
            challenge_token=token,
            rp_id="localhost",
            expected_origin="http://localhost:8000",
            creds=creds,
        )
        assert result is None

    def test_verification_failure(self):
        token = "test-auth-verify-fail"
        _challenges[token] = b"challenge-bytes"
        creds = [
            {
                "id": "matching-id",
                "public_key": "dGVzdC1waw",
                "sign_count": 0,
                "name": "Key",
                "registered_at": "2025-01-01T00:00:00",
            }
        ]
        result = complete_authentication(
            credential_json='{"id": "matching-id", "rawId": "matching-id"}',
            challenge_token=token,
            rp_id="localhost",
            expected_origin="http://localhost:8000",
            creds=creds,
        )
        assert result is None
