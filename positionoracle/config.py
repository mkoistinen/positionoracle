"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """PositionOracle configuration sourced from environment / ``.env`` file.

    Attributes
    ----------
    secret_key : str
        Secret used for signing session cookies and WebAuthn challenges.
    setup_token : str
        One-time token that gates initial passkey registration.
    rp_id : str
        WebAuthn Relying Party ID (must match the domain in production).
    rp_name : str
        Human-readable Relying Party name shown during passkey registration.
    expected_origin : str
        Expected origin for WebAuthn ceremony verification.
    flex_token : str
        IB Flex Web Service API token.
    query_id : str
        IB Flex Query ID.
    massive_api_key : str
        Massive (formerly Polygon) API key for market data.
    fred_api_key : str
        FRED API key for treasury yield lookups (used by VRP entry-IV
        inversion).
    anthropic_api_key : str
        Anthropic API key for Claude analysis.
    claude_model : str
        Claude model to use for analysis (e.g. claude-haiku-4-5-20251001,
        claude-sonnet-4-6, claude-opus-4-6).
    data_dir : Path
        Directory for persistent data (SQLite DB, credentials).
    option_spread_pct : float
        Assumed full bid/ask spread as a fraction of the option mid,
        used to haircut P&L toward a realizable exit when live quotes
        are unavailable. Default 0.05 (5%).
    option_commission_per_contract : float
        Per-contract commission subtracted from the exit mark. Default
        0.65 (typical IB rate).
    """

    secret_key: str = "CHANGE-ME"
    setup_token: str = "CHANGE-ME"
    rp_id: str = "localhost"
    rp_name: str = "PositionOracle"
    expected_origin: str = "http://localhost:8000"
    flex_token: str = ""
    query_id: str = ""
    massive_api_key: str = ""
    fred_api_key: str = ""
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    data_dir: Path = Path("/app/data")

    # Option P&L exit-friction model. When live bid/ask is unavailable
    # (common on Greeks-only Massive tiers) the theoretical mid is
    # haircut by half of ``option_spread_pct`` toward the side you must
    # cross to close, and ``option_commission_per_contract`` is
    # subtracted, so P&L reflects a realizable liquidation value rather
    # than a frictionless mid.
    option_spread_pct: float = 0.05
    option_commission_per_contract: float = 0.65

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    """Return the application settings (cached by callers as needed).

    Returns
    -------
    Settings
        Populated settings instance.
    """
    return Settings()
