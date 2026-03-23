"""Black-Scholes Greeks calculator for first- and second-order derivatives."""

from __future__ import annotations

import math

from scipy.stats import norm

from positionoracle.types import ContractType, Greeks


def _d1(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """Compute Black-Scholes d1.

    Parameters
    ----------
    s : float
        Underlying price.
    k : float
        Strike price.
    t : float
        Time to expiration in years.
    r : float
        Risk-free interest rate.
    sigma : float
        Implied volatility.

    Returns
    -------
    float
        The d1 value.
    """
    return (math.log(s / k) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))


def _d2(d1: float, sigma: float, t: float) -> float:
    """Compute Black-Scholes d2.

    Parameters
    ----------
    d1 : float
        The d1 value.
    sigma : float
        Implied volatility.
    t : float
        Time to expiration in years.

    Returns
    -------
    float
        The d2 value.
    """
    return d1 - sigma * math.sqrt(t)


def compute_greeks(
    s: float,
    k: float,
    t: float,
    r: float,
    sigma: float,
    contract_type: ContractType,
) -> Greeks:
    """Compute all first- and second-order Greeks for an option.

    Parameters
    ----------
    s : float
        Current underlying price.
    k : float
        Strike price.
    t : float
        Time to expiration in years (must be > 0).
    r : float
        Risk-free interest rate (e.g. 0.05 for 5%).
    sigma : float
        Implied volatility (e.g. 0.25 for 25%).
    contract_type : ContractType
        Call or put.

    Returns
    -------
    Greeks
        All computed Greeks. Theta is expressed per calendar day.
    """
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return Greeks(implied_volatility=sigma)

    sqrt_t = math.sqrt(t)
    d1 = _d1(s, k, t, r, sigma)
    d2 = _d2(d1, sigma, t)

    n_d1 = norm.cdf(d1)
    n_d2 = norm.cdf(d2)
    nprime_d1 = norm.pdf(d1)

    # -- First-order Greeks --
    if contract_type == ContractType.CALL:
        delta = n_d1
        theta_annual = (
            -(s * nprime_d1 * sigma) / (2 * sqrt_t)
            - r * k * math.exp(-r * t) * n_d2
        )
    else:
        delta = n_d1 - 1.0
        theta_annual = (
            -(s * nprime_d1 * sigma) / (2 * sqrt_t)
            + r * k * math.exp(-r * t) * norm.cdf(-d2)
        )

    gamma = nprime_d1 / (s * sigma * sqrt_t)
    vega = s * nprime_d1 * sqrt_t / 100  # Per 1% move in vol
    theta = theta_annual / 365  # Per calendar day

    # -- Second-order Greeks --
    vanna = -nprime_d1 * d2 / sigma
    charm_annual = -nprime_d1 * (
        2 * r * t - d2 * sigma * sqrt_t
    ) / (2 * t * sigma * sqrt_t)
    if contract_type == ContractType.PUT:
        charm_annual += r * math.exp(-r * t) * norm.cdf(-d2)
        # Correction: charm for puts needs adjustment
        # Actually the charm formula above already handles the full derivative
        # Let's use the standard form
        charm_annual = -nprime_d1 * (
            2 * r * t - d2 * sigma * sqrt_t
        ) / (2 * t * sigma * sqrt_t)
    charm = charm_annual / 365  # Per calendar day

    vomma = vega * d1 * d2 / sigma

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        vanna=vanna,
        charm=charm,
        vomma=vomma,
        implied_volatility=sigma,
    )


def compute_greeks_from_massive(
    s: float,
    k: float,
    t: float,
    r: float,
    contract_type: ContractType,
    delta: float,
    gamma: float,
    theta: float,
    vega: float,
    iv: float,
) -> Greeks:
    """Build a full Greeks object using Massive first-order + computed second-order.

    Uses Massive-provided first-order Greeks directly and computes
    second-order Greeks (vanna, charm, vomma) from the IV and underlying price.

    Parameters
    ----------
    s : float
        Current underlying price.
    k : float
        Strike price.
    t : float
        Time to expiration in years.
    r : float
        Risk-free interest rate.
    contract_type : ContractType
        Call or put.
    delta : float
        Massive-provided delta.
    gamma : float
        Massive-provided gamma.
    theta : float
        Massive-provided theta (per day).
    vega : float
        Massive-provided vega.
    iv : float
        Massive-provided implied volatility.

    Returns
    -------
    Greeks
        Combined first-order (Massive) and second-order (computed) Greeks.
    """
    if t <= 0 or iv <= 0 or s <= 0 or k <= 0:
        return Greeks(
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            implied_volatility=iv,
        )

    # Compute second-order from Black-Scholes using the provided IV
    sqrt_t = math.sqrt(t)
    d1 = _d1(s, k, t, r, iv)
    d2 = _d2(d1, iv, t)
    nprime_d1 = norm.pdf(d1)

    vanna = -nprime_d1 * d2 / iv
    charm_annual = -nprime_d1 * (
        2 * r * t - d2 * iv * sqrt_t
    ) / (2 * t * iv * sqrt_t)
    charm = charm_annual / 365

    # Vega for vomma calc needs to be in same units
    vega_raw = s * nprime_d1 * sqrt_t
    vomma = vega_raw * d1 * d2 / iv / 100  # Scaled to match vega per 1%

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        vanna=vanna,
        charm=charm,
        vomma=vomma,
        implied_volatility=iv,
    )
