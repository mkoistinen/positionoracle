"""Shared types for the PositionOracle application."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import datetime

    SymbolLoss = tuple[str, datetime.date]
    """``(underlying_symbol, trade_date)`` pair for a realized-loss closing trade."""


class ContractType(Enum):
    """Position type."""

    CALL = "call"
    PUT = "put"
    STOCK = "stock"


@dataclass(frozen=True, slots=True)
class Position:
    """An options position loaded from an IB Flex Query.

    Attributes
    ----------
    symbol : str
        The OCC-style option symbol or underlying ticker.
    underlying : str
        Underlying ticker (e.g. ``"AAPL"``).
    contract_type : ContractType
        Call or put.
    strike : float
        Strike price.
    expiration : datetime.date
        Contract expiration date.
    quantity : int
        Number of contracts (negative for short).
    cost_basis : float
        Total cost basis for the position.
    multiplier : int
        Contract multiplier (typically 100).
    """

    symbol: str
    underlying: str
    contract_type: ContractType
    strike: float
    expiration: datetime.date
    quantity: int
    cost_basis: float
    multiplier: int = 100


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A persisted API key for programmatic access.

    The cleartext key is never stored — only the SHA-256 hash. A short
    prefix is retained so the user can identify which key is which
    after the cleartext is shown once at generation time.

    Attributes
    ----------
    id : int
        Database row ID (used in revoke URLs).
    name : str
        User-supplied label (e.g. ``"reporting-server"``).
    key_prefix : str
        First 8 characters of the cleartext key — displayed in lists
        to help the user identify a row.
    key_hash : str
        SHA-256 hex digest of the full cleartext key.
    created_at : datetime.datetime
        UTC creation timestamp.
    last_used_at : datetime.datetime | None
        UTC timestamp of the most recent successful auth using this
        key, or ``None`` if never used.
    """

    id: int
    name: str
    key_prefix: str
    key_hash: str
    created_at: datetime.datetime
    last_used_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    """A wash-sale blacklist entry.

    Attributes
    ----------
    symbol : str
        Underlying ticker (always upper-cased).
    loss_date : datetime.date
        Most recent realized-loss date for this symbol.
    expires : datetime.date
        ``loss_date + 30`` — the IRS wash-sale window end.
    """

    symbol: str
    loss_date: datetime.date
    expires: datetime.date


@dataclass(frozen=True, slots=True)
class PositionEntry:
    """Cached entry-time data for an option position.

    Persisted in the ``position_entry`` table and used by the VRP
    calculation to compare current realized vol against the IV
    implied by the option's entry premium.

    Attributes
    ----------
    symbol : str
        Option contract symbol (matches ``Position.symbol``).
    underlying : str
        Underlying ticker.
    entry_time : datetime.datetime
        Aware (ET) timestamp of the opening trade.
    entry_spot : float
        Underlying spot at entry, taken as ``(H+L)/2`` of the 1-min
        bar covering ``entry_time``.
    entry_premium_per_share : float
        Per-share option premium at entry (positive number).
    entry_iv : float | None
        Implied vol back-solved from the entry premium, or ``None``
        if the inversion failed (price outside bracket).
    entry_rate : float
        Continuously-compounded risk-free rate used for the inversion.
    computed_at : datetime.datetime
        When this record was computed.
    """

    symbol: str
    underlying: str
    entry_time: datetime.datetime
    entry_spot: float
    entry_premium_per_share: float
    entry_iv: float | None
    entry_rate: float
    computed_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class OpeningTrade:
    """An opening trade for an option contract.

    Used to anchor VRP calculations to the moment the contract was
    written (or purchased). For multi-lot opens we keep the earliest
    trade and rely on cost-basis-weighted premium from the
    ``OpenPosition`` record for the per-share entry price.

    Attributes
    ----------
    symbol : str
        Option contract symbol (matches ``Position.symbol``).
    underlying : str
        Underlying ticker.
    trade_datetime : datetime.datetime
        Timezone-aware (America/New_York) trade timestamp.
    trade_price : float
        Per-share trade price for the option (the premium paid or
        received, in dollars per share — multiply by 100 for the
        per-contract value).
    quantity : int
        Signed quantity at this trade (negative for sells).
    """

    symbol: str
    underlying: str
    trade_datetime: datetime.datetime
    trade_price: float
    quantity: int


@dataclass(frozen=True, slots=True)
class FlexReport:
    """A parsed IB Flex Query report with metadata.

    Attributes
    ----------
    when_generated : datetime.datetime
        Timezone-aware (America/New_York) timestamp IB stamped on the
        report at generation time. Falls back to "now" if absent.
    positions : list[Position]
        Parsed positions from the ``OpenPositions`` section.
    losses : list[SymbolLoss]
        ``(underlying_symbol, trade_date)`` pairs for each closing
        trade with negative realized P&L. Drives the wash-sale tracker.
    opening_trades : dict[str, OpeningTrade]
        Earliest opening trade per option symbol. Drives the VRP
        entry-data backfill.
    """

    when_generated: datetime.datetime
    positions: list[Position]
    losses: list[SymbolLoss]
    opening_trades: dict[str, OpeningTrade] = field(default_factory=dict)


@dataclass(slots=True)
class Greeks:
    """First- and second-order option Greeks.

    Attributes
    ----------
    delta : float
        Rate of change of option price with respect to underlying price.
    gamma : float
        Rate of change of delta with respect to underlying price.
    theta : float
        Rate of change of option price with respect to time (per day).
    vega : float
        Rate of change of option price with respect to volatility.
    vanna : float
        Rate of change of delta with respect to volatility.
    charm : float
        Rate of change of delta with respect to time (per day).
    vomma : float
        Rate of change of vega with respect to volatility.
    implied_volatility : float
        Implied volatility as a decimal.
    """

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    vanna: float = 0.0
    charm: float = 0.0
    vomma: float = 0.0
    implied_volatility: float = 0.0


@dataclass(slots=True)
class PositionGreeks:
    """A position combined with its live Greeks and market data.

    Attributes
    ----------
    position : Position
        The underlying position.
    greeks : Greeks
        Current Greeks for one contract.
    underlying_price : float
        Current price of the underlying.
    option_mid : float | None
        Mid price from quotes (if available; bid/ask may be absent on
        delayed/Greeks-only Massive tiers).
    theoretical_mid : float | None
        Black-Scholes theoretical price computed from the current spot,
        live IV, time to expiration, and rate. Always populated when
        the inputs are valid; used as the canonical "current value"
        for P&L computation since quote-based mid is tier-dependent.
    pnl_pct : float | None
        Direction-aware P&L as a fraction of the entry premium per
        share. Positive = position is up; negative = down. For shorts:
        ``(entry_premium - current_value) / entry_premium``. For longs:
        ``(current_value - entry_premium) / entry_premium``. ``None``
        when ``entry_premium_per_share`` or the theoretical mid is
        unavailable.
    vrp : float | None
        Volatility Risk Premium ratio sigma_RV / sigma_IV(entry).
        ``None`` if entry data is missing or RV cannot yet be computed.
        Below 1.0 is favorable for short positions; above 1.0 is
        favorable for long positions.
    entry_iv : float | None
        The sigma_IV inverted from the entry premium -- the IV the
        writer was compensated for.
    rv : float | None
        Trailing-window realized vol used for the current VRP.
    rv_window_days : int
        Number of trading-day returns used in the RV calculation.
    """

    position: Position
    greeks: Greeks
    underlying_price: float = 0.0
    option_mid: float | None = None
    theoretical_mid: float | None = None
    pnl_pct: float | None = None
    vrp: float | None = None
    entry_iv: float | None = None
    rv: float | None = None
    rv_window_days: int = 0


class AdviceLevel(Enum):
    """Severity level for adjustment advice."""

    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"


@dataclass(frozen=True, slots=True)
class Advice:
    """An adjustment or exit recommendation for a position.

    Attributes
    ----------
    level : AdviceLevel
        Severity of the recommendation.
    message : str
        Human-readable advice.
    position_symbol : str
        The option symbol this advice applies to.
    metric : str
        Which Greek or metric triggered this advice.
    value : float
        The current value of the triggering metric.
    threshold : float
        The threshold that was breached.
    """

    level: AdviceLevel
    message: str
    position_symbol: str
    metric: str
    value: float
    threshold: float


@dataclass(slots=True)
class PortfolioSummary:
    """Aggregated Greeks across all positions for a single underlying.

    Attributes
    ----------
    underlying : str
        Underlying ticker.
    net_delta : float
        Sum of position-weighted deltas.
    net_gamma : float
        Sum of position-weighted gammas.
    net_theta : float
        Sum of position-weighted thetas.
    net_vega : float
        Sum of position-weighted vegas.
    positions : list[PositionGreeks]
        Individual position details.
    advice : list[Advice]
        Active recommendations.
    """

    underlying: str
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_theta: float = 0.0
    net_vega: float = 0.0
    positions: list[PositionGreeks] = field(default_factory=list)
    advice: list[Advice] = field(default_factory=list)


class CredentialRecord(TypedDict):
    """JSON-serializable representation of a stored WebAuthn credential."""

    id: str
    public_key: str
    sign_count: int
    name: str
    registered_at: str


@dataclass(frozen=True, slots=True)
class GEXStrike:
    """GEX data for a single strike price.

    Attributes
    ----------
    strike : float
        Strike price.
    call_gex : float
        Call gamma exposure at this strike (dollar-gamma).
    put_gex : float
        Put gamma exposure at this strike (dollar-gamma).
    net_gex : float
        Net GEX (call_gex + put_gex).
    call_oi : int
        Total call open interest at this strike.
    put_oi : int
        Total put open interest at this strike.
    """

    strike: float
    call_gex: float
    put_gex: float
    net_gex: float
    call_oi: int
    put_oi: int


@dataclass(slots=True)
class GEXProfile:
    """GEX profile for an underlying across all strikes.

    Attributes
    ----------
    underlying : str
        Underlying ticker.
    spot_price : float
        Current underlying price.
    strikes : list[GEXStrike]
        Per-strike GEX data, sorted by strike.
    net_gex : float
        Total net GEX across all strikes.
    call_wall : float
        Strike with highest absolute call GEX (resistance).
    put_wall : float
        Strike with highest absolute put GEX (support).
    flip_point : float
        Strike where cumulative GEX flips from positive to negative.
    expirations : list[str]
        Expiration dates included in this profile.
    fetched_at : str
        ISO timestamp when data was fetched.
    """

    underlying: str
    spot_price: float
    strikes: list[GEXStrike] = field(default_factory=list)
    net_gex: float = 0.0
    call_wall: float = 0.0
    put_wall: float = 0.0
    flip_point: float = 0.0
    expirations: list[str] = field(default_factory=list)
    fetched_at: str = ""


# Type alias for the full list of stored credentials.
CredentialStore = list[CredentialRecord]
