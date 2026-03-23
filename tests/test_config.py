from pathlib import Path

from positionoracle.config import Settings, get_settings


class TestSettings:
    def test_defaults(self, monkeypatch):
        # Clear env vars that would override defaults
        for key in (
            "SECRET_KEY", "SETUP_TOKEN", "RP_ID", "RP_NAME",
            "EXPECTED_ORIGIN", "FLEX_TOKEN", "QUERY_ID",
            "MASSIVE_API_KEY", "DATA_DIR",
        ):
            monkeypatch.delenv(key, raising=False)

        s = Settings(_env_file=None)
        assert s.secret_key == "CHANGE-ME"
        assert s.rp_id == "localhost"
        assert s.rp_name == "PositionOracle"
        assert s.expected_origin == "http://localhost:8000"
        assert s.flex_token == ""
        assert s.query_id == ""
        assert s.massive_api_key == ""
        assert s.data_dir == Path("/app/data")

    def test_get_settings_returns_instance(self):
        s = get_settings()
        assert isinstance(s, Settings)

    def test_custom_values(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret")
        monkeypatch.setenv("RP_ID", "example.com")
        monkeypatch.setenv("MASSIVE_API_KEY", "pk_test")
        monkeypatch.setenv("FLEX_TOKEN", "flex-test")
        monkeypatch.setenv("QUERY_ID", "12345")
        s = Settings(_env_file=None)
        assert s.secret_key == "test-secret"
        assert s.rp_id == "example.com"
        assert s.massive_api_key == "pk_test"
        assert s.flex_token == "flex-test"
        assert s.query_id == "12345"
