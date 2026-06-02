"""Volatility Risk Premium (VRP) computations.

VRP = sigma_RV / sigma_IV, where sigma_RV is the trailing realized vol
of the underlying and sigma_IV is the implied vol embedded in the
option's *entry* premium. For an options writer:

- VRP > 1.0 -- the underlying is realizing more vol than was priced in
  at entry; you are underpaid for the risk you are carrying.
- VRP < 1.0 -- the realized vol is below what was priced in; you are
  collecting the premium as intended.

For a long-options holder, the interpretation flips.
"""

from __future__ import annotations

import itertools
import logging
import math
from typing import TYPE_CHECKING

from scipy.optimize import brentq
from scipy.stats import norm

from positionoracle.types import ContractType

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Number of trading days per year used to annualize realized vol.
_TRADING_DAYS_PER_YEAR = 252

# Default trailing window for realized vol (≈ one month of sessions).
DEFAULT_RV_WINDOW = 21


def realized_vol_annualized(
    closes: Sequence[float],
    window: int = DEFAULT_RV_WINDOW,
) -> float:
    """Compute annualized realized volatility from a series of closes.

    Uses the close-to-close log-return convention with the standard
    ``sqrt(252/N * Σ r_i²)`` annualization. Returns are computed across
    *all* supplied closes; the ``window`` argument caps the input series
    to its trailing ``window+1`` entries (one extra close is required to
    produce ``window`` returns).

    Parameters
    ----------
    closes : Sequence[float]
        Daily close prices, oldest first. Must contain at least 2
        entries.
    window : int
        Number of trailing returns to use. Defaults to 21 (~1 month).

    Returns
    -------
    float
        Annualized realized volatility as a decimal (e.g. ``0.25`` for
        25 percent). Returns ``nan`` if there is insufficient data.
    """
    if len(closes) < 2:
        return float("nan")

    # Cap to the trailing window+1 closes so we get exactly `window`
    # returns when possible.
    needed = window + 1
    series = list(closes[-needed:]) if len(closes) > needed else list(closes)

    returns: list[float] = []
    for prev, curr in itertools.pairwise(series):
        if prev <= 0 or curr <= 0:
            continue
        returns.append(math.log(curr / prev))

    n = len(returns)
    if n < 2:
        return float("nan")

    sum_sq = sum(r * r for r in returns)
    return math.sqrt((_TRADING_DAYS_PER_YEAR / n) * sum_sq)


def bs_price(
    s: float,
    k: float,
    t: float,
    r: float,
    sigma: float,
    contract_type: ContractType,
    q: float = 0.0,
) -> float:
    """Black-Scholes price for a European option.

    Parameters
    ----------
    s : float
        Underlying spot.
    k : float
        Strike.
    t : float
        Time to expiration in years.
    r : float
        Continuously-compounded risk-free rate.
    sigma : float
        Volatility (decimal).
    contract_type : ContractType
        Call or put.
    q : float
        Continuous dividend yield. Default 0.

    Returns
    -------
    float
        Theoretical option price.
    """
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return 0.0

    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)

    if contract_type == ContractType.CALL:
        return s * disc_q * norm.cdf(d1) - k * disc_r * norm.cdf(d2)
    return k * disc_r * norm.cdf(-d2) - s * disc_q * norm.cdf(-d1)


def implied_vol(
    market_price: float,
    s: float,
    k: float,
    t: float,
    r: float,
    contract_type: ContractType,
    q: float = 0.0,
    *,
    vol_bracket: tuple[float, float] = (1e-4, 5.0),
    tol: float = 1e-6,
) -> float:
    """Solve Black-Scholes for implied volatility via Brent's method.

    Parameters
    ----------
    market_price : float
        Observed option price (per share — not per contract).
    s : float
        Underlying spot at the observation time.
    k : float
        Strike.
    t : float
        Time to expiration in years.
    r : float
        Continuously-compounded risk-free rate.
    contract_type : ContractType
        Call or put.
    q : float
        Continuous dividend yield. Default 0.
    vol_bracket : (float, float)
        Search bracket. Defaults span 0.01 percent to 500 percent --
        covers every realistic equity/ETF option.
    tol : float
        Absolute tolerance on sigma.

    Returns
    -------
    float
        Implied vol (decimal, annualized), or ``nan`` if the market
        price lies outside the bracketed price range (e.g. below
        intrinsic from a stale quote).

    Notes
    -----
    Brent's method is used in preference to Newton-Raphson: it cannot
    diverge and does not need vega, which matters for deep ITM/OTM
    strikes where vega approaches 0.
    """
    if market_price <= 0 or t <= 0 or s <= 0 or k <= 0:
        return float("nan")

    def objective(sigma: float) -> float:
        return bs_price(s, k, t, r, sigma, contract_type, q) - market_price

    lo, hi = vol_bracket
    f_lo = objective(lo)
    f_hi = objective(hi)
    if f_lo * f_hi > 0:
        logger.debug(
            "implied_vol: market_price=%.4f outside bracket f_lo=%.4f f_hi=%.4f "
            "(S=%.2f K=%.2f T=%.4f r=%.4f type=%s)",
            market_price, f_lo, f_hi, s, k, t, r, contract_type.value,
        )
        return float("nan")
    try:
        return brentq(objective, lo, hi, xtol=tol)
    except (ValueError, RuntimeError):
        logger.exception("implied_vol: brentq failed")
        return float("nan")


def vrp_ratio(rv: float, iv: float) -> float:
    """Return sigma_RV / sigma_IV, or ``nan`` if either input is non-positive."""
    if rv <= 0 or iv <= 0 or math.isnan(rv) or math.isnan(iv):
        return float("nan")
    return rv / iv
