"""Shared types for the PositionOracle application."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import datetime


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
        Mid price of the option (if available).
    """

    position: Position
    greeks: Greeks
    underlying_price: float = 0.0
    option_mid: float | None = None


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
