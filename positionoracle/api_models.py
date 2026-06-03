"""Pydantic response models for the public REST API.

These models drive the OpenAPI schema served at ``/docs`` and ``/redoc``.
They mirror the JSON shape that the WebSocket already broadcasts so any
consumer written against the WS feed can also consume the REST endpoints
unchanged.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------


class CreateApiKeyRequest(BaseModel):
    """Body for ``POST /api/keys``."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Human-readable label for the key (e.g. \"reporting-server\").",
    )


class ApiKeyCreated(BaseModel):
    """Response for ``POST /api/keys``. The cleartext ``key`` is shown once."""

    id: int
    name: str
    key_prefix: str = Field(
        ...,
        description="First 8 characters of the cleartext key — stored for "
        "identification in management lists.",
    )
    key: str = Field(
        ...,
        description="The cleartext API key. Copy this NOW — it cannot be "
        "retrieved again.",
    )
    created_at: datetime.datetime


class ApiKeyListItem(BaseModel):
    """A row returned from ``GET /api/keys`` (no cleartext)."""

    id: int
    name: str
    key_prefix: str
    created_at: datetime.datetime
    last_used_at: datetime.datetime | None = None


class ApiKeyList(BaseModel):
    """Wrapper for ``GET /api/keys``."""

    keys: list[ApiKeyListItem]


# ---------------------------------------------------------------------------
# v1 — Positions response (mirrors the WebSocket payload)
# ---------------------------------------------------------------------------


class GreeksModel(BaseModel):
    """First- and second-order Greeks for a single option contract."""

    delta: float
    gamma: float
    theta: float
    vega: float
    vanna: float
    charm: float
    vomma: float
    implied_volatility: float


class PositionModel(BaseModel):
    """A position with live Greeks and derived metrics."""

    symbol: str
    underlying: str
    contract_type: str = Field(..., description="``call``, ``put``, or ``stock``.")
    strike: float
    expiration: str = Field(..., description="ISO date.")
    quantity: int = Field(..., description="Negative for short positions.")
    cost_basis: float
    multiplier: int
    underlying_price: float
    option_mid: float | None = Field(
        None,
        description="Quote-derived mid; may be ``null`` on Massive tiers without "
        "bid/ask data.",
    )
    theoretical_mid: float | None = Field(
        None,
        description="Black-Scholes price from live IV. Always populated when "
        "inputs are valid; used as the canonical current value.",
    )
    pnl_pct: float | None = Field(
        None,
        description="Direction-aware P&L as a fraction of entry premium. "
        "Positive = position is up.",
    )
    greeks: GreeksModel
    vrp: float | None = Field(
        None,
        description="Volatility Risk Premium ratio (trailing 21-day RV / entry "
        "IV). Below 1.0 favors shorts.",
    )
    entry_iv: float | None = None
    rv: float | None = None
    rv_window_days: int = 0


class AdviceModel(BaseModel):
    """An advisory item attached to a position or underlying."""

    level: str = Field(..., description="``info``, ``warning``, or ``urgent``.")
    message: str
    position_symbol: str
    metric: str
    value: float
    threshold: float


class UnderlyingSummary(BaseModel):
    """Aggregated Greeks and positions for a single underlying."""

    net_delta: float
    net_gamma: float
    net_theta: float
    net_vega: float
    beta: float
    beta_weighted_delta: float
    positions: list[PositionModel]
    advice: list[AdviceModel]


class PortfolioRollup(BaseModel):
    """Portfolio-level rollup across all underlyings."""

    net_delta: float
    net_gamma: float
    net_theta: float
    net_vega: float
    beta_weighted_delta: float
    spy_price: float


class GEXStrikeModel(BaseModel):
    """GEX data for a single strike."""

    strike: float
    call_gex: float
    put_gex: float
    net_gex: float
    call_oi: int
    put_oi: int


class GEXProfileModel(BaseModel):
    """GEX profile for one underlying."""

    underlying: str
    spot_price: float
    net_gex: float
    call_wall: float
    put_wall: float
    flip_point: float
    expirations: list[str]
    fetched_at: str
    strikes: list[GEXStrikeModel]


class PositionsResponse(BaseModel):
    """Response for ``GET /api/v1/positions``.

    The shape is byte-identical to the payload broadcast over the
    WebSocket — same field names, same types, same nesting.
    """

    type: str = "update"
    last_updated: str
    last_report_generated: str | None = None
    market_open: bool
    underlyings: dict[str, UnderlyingSummary]
    portfolio: PortfolioRollup
    gex: dict[str, GEXProfileModel] | None = None


# ---------------------------------------------------------------------------
# v1 — Wash-sale response
# ---------------------------------------------------------------------------


class BlacklistEntryModel(BaseModel):
    """One row from the wash-sale blacklist."""

    symbol: str
    loss_date: str = Field(..., description="ISO date of the realizing trade.")
    expires: str = Field(..., description="ISO date when the 30-day window closes.")
    days_remaining: int


class WashsaleResponse(BaseModel):
    """Response for ``GET /api/v1/washsale``."""

    entries: list[BlacklistEntryModel]
    last_report_generated: str | None = None


# ---------------------------------------------------------------------------
# v1 — Create / delete positions
# ---------------------------------------------------------------------------


class CreatePositionRequest(BaseModel):
    """Body for ``POST /api/v1/positions``.

    Use this to record an intraday trade before it shows up in IB's
    next Flex report. On insert the server synchronously:

    1. Fetches the 1-min underlying bar covering ``entry_time`` from
       Massive and uses ``(H+L)/2`` as the entry spot.
    2. Fetches the matching short-tenor Treasury yield from FRED.
    3. Inverts Black-Scholes to compute the entry IV.
    4. Triggers a snapshot refresh so live Greeks / VRP / P&L%
       populate before the response returns.

    **Reconciliation note:** the next Flex sync upserts by symbol. If
    IB's report contains the same contract, the row is updated in
    place. If IB hasn't booked the trade yet, the sync's
    ``DELETE ... WHERE symbol NOT IN (...)`` step will remove the
    manual entry. Wait to sync until IB has caught up.
    """

    underlying: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Underlying ticker, e.g. ``AAPL``.",
    )
    contract_type: str = Field(
        ...,
        pattern="^(call|put|stock)$",
        description="``call``, ``put``, or ``stock``.",
    )
    quantity: int = Field(
        ...,
        description="Signed contract count. Negative = short.",
    )
    entry_time: datetime.datetime = Field(
        ...,
        description="Trade timestamp. Tz-aware ISO 8601 strongly "
        "recommended; naive timestamps are assumed to be ET.",
    )
    entry_premium_per_share: float = Field(
        ...,
        gt=0,
        description="Per-share premium paid (long) or received "
        "(short). Always positive.",
    )
    strike: float | None = Field(
        None,
        gt=0,
        description="Required for option contracts; ignored for stock.",
    )
    expiration: datetime.date | None = Field(
        None,
        description="Required for option contracts; ignored for stock.",
    )
    multiplier: int | None = Field(
        None,
        ge=1,
        description="Contract multiplier. Defaults to 100 for options "
        "and 1 for stock.",
    )


class CreatedPositionResponse(BaseModel):
    """Response for ``POST /api/v1/positions``.

    Returns the symbol the server assigned (IB OCC-style for options,
    bare ticker for stock) along with the computed entry data.
    """

    symbol: str
    underlying: str
    contract_type: str
    strike: float
    expiration: str
    quantity: int
    multiplier: int
    entry_time: datetime.datetime | None = None
    entry_spot: float | None = None
    entry_premium_per_share: float | None = None
    entry_iv: float | None = None
    entry_rate: float | None = None
