"""Trade planning: VRP=1.0 fair pricing for hypothetical option trades.

Given an underlying's trailing realized vol (RV), the current spot, a
risk-free rate, and a contract (strike / expiry / call-put), compute the
Black-Scholes price at which implied vol would exactly equal realized vol
-- i.e. VRP == 1.0. That price is the fair-value anchor for entering a
trade:

- A short (seller) wants to *collect at least* the VRP=1.0 price: getting
  more means IV > RV (VRP < 1), so the market is paying you above the
  risk the underlying is actually realizing.
- A long (buyer) wants to *pay no more than* the VRP=1.0 price: paying
  less means RV > IV (VRP > 1), so the option is cheap versus what is
  realizing.

When a live IV is supplied (from a current market snapshot) the quote
also reports the *current* VRP (RV / IV_live) and a direction-aware
verdict on whether today's market is favorable for the intended side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from positionoracle import vrp

if TYPE_CHECKING:
    from positionoracle.types import ContractType

# Neutral band around VRP=1.0 within which neither side has a meaningful
# realized-vs-implied edge (±5%).
_NEUTRAL_BAND = 0.05


@dataclass(frozen=True, slots=True)
class VrpQuote:
    """A VRP=1.0 fair price for one contract, with optional live context.

    Attributes
    ----------
    strike : float
        Strike this quote prices.
    fair_price : float
        VRP=1.0 (IV == RV) Black-Scholes price, per share.
    fair_price_contract : float
        ``fair_price`` times the multiplier (per-contract dollars).
    live_iv : float | None
        Current market implied vol, if a snapshot was available.
    live_mid : float | None
        Current market mid (per share), if quotes were available.
    current_vrp : float | None
        ``RV / live_iv`` -- the VRP the market is currently offering.
        ``None`` when no live IV was available.
    signal : str
        ``favorable`` / ``neutral`` / ``unfavorable`` for the intended
        direction, or ``na`` when there is no live IV to judge.
    verdict : str
        Human-readable one-liner explaining the quote.
    """

    strike: float
    fair_price: float
    fair_price_contract: float
    live_iv: float | None
    live_mid: float | None
    current_vrp: float | None
    signal: str
    verdict: str


def _signal_for(direction: str, current_vrp: float) -> str:
    """Classify the current VRP for the intended direction.

    Longs want VRP > 1 (RV above IV -> option cheap); shorts want
    VRP < 1 (IV above RV -> premium rich). A ±5% band around 1.0 is
    treated as roughly fair.
    """
    hi = 1.0 + _NEUTRAL_BAND
    lo = 1.0 - _NEUTRAL_BAND
    if direction == "long":
        if current_vrp >= hi:
            return "favorable"
        if current_vrp <= lo:
            return "unfavorable"
        return "neutral"
    # short
    if current_vrp <= lo:
        return "favorable"
    if current_vrp >= hi:
        return "unfavorable"
    return "neutral"


def _verdict(
    *,
    direction: str,
    signal: str,
    fair: float,
    current_vrp: float | None,
    live_iv: float | None,
    live_mid: float | None,
    rv: float,
) -> str:
    """Compose the human-readable one-liner for a quote."""
    fair_str = f"${fair:.2f}"
    action = "collect" if direction == "short" else "pay"
    bound = "at least" if direction == "short" else "no more than"
    aim = f"As a {direction}, {action} {bound} {fair_str}/share to keep realized vol on your side."

    if current_vrp is None:
        return (
            f"VRP=1.0 fair value {fair_str}/share. {aim} "
            "(No live IV available to gauge today's market.)"
        )

    mid_str = f"${live_mid:.2f}" if live_mid is not None else "n/a"
    context = (
        f"Live mid {mid_str}, IV {live_iv * 100:.0f}% vs RV {rv * 100:.0f}% "
        f"(VRP {current_vrp:.2f})."
    )
    if signal == "favorable":
        edge = (
            "rich vs realized — good premium to sell"
            if direction == "short"
            else "cheap vs realized — good value to buy"
        )
        return f"{context} {edge.capitalize()}. {aim}"
    if signal == "unfavorable":
        edge = (
            "underpaid for the risk — poor premium to sell"
            if direction == "short"
            else "expensive vs realized — you'd overpay"
        )
        return f"{context} {edge.capitalize()}. {aim}"
    return f"{context} Roughly fair (IV ≈ RV). {aim}"


def price_quote(
    *,
    spot: float,
    strike: float,
    dte_days: int,
    rate: float,
    rv: float,
    contract_type: ContractType,
    direction: str,
    live_iv: float | None = None,
    live_mid: float | None = None,
    multiplier: int = 100,
) -> VrpQuote:
    """Price one contract at VRP=1.0 and judge it against the live market.

    Parameters
    ----------
    spot : float
        Current underlying price.
    strike : float
        Contract strike.
    dte_days : int
        Calendar days to expiration (floored at 1 for the BS clock).
    rate : float
        Continuously-compounded risk-free rate for the horizon.
    rv : float
        Trailing annualized realized vol of the underlying (the sigma
        that makes VRP == 1.0).
    contract_type : ContractType
        Call or put.
    direction : str
        ``"long"`` or ``"short"`` -- the side being planned.
    live_iv : float | None
        Current market IV, if known, used to report the current VRP.
    live_mid : float | None
        Current market mid (per share), if known, for context.
    multiplier : int
        Contract multiplier (default 100).

    Returns
    -------
    VrpQuote
        The fair price plus direction-aware verdict.
    """
    t_years = max(dte_days / 365.0, 1.0 / 365.0)
    fair = vrp.bs_price(
        s=spot, k=strike, t=t_years, r=rate, sigma=rv, contract_type=contract_type,
    )

    current_vrp: float | None = None
    if live_iv is not None and live_iv > 0 and rv > 0:
        current_vrp = rv / live_iv

    signal = "na" if current_vrp is None else _signal_for(direction, current_vrp)
    verdict = _verdict(
        direction=direction,
        signal=signal,
        fair=fair,
        current_vrp=current_vrp,
        live_iv=live_iv,
        live_mid=live_mid,
        rv=rv,
    )

    return VrpQuote(
        strike=strike,
        fair_price=fair,
        fair_price_contract=fair * multiplier,
        live_iv=live_iv,
        live_mid=live_mid,
        current_vrp=current_vrp,
        signal=signal,
        verdict=verdict,
    )
