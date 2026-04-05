"""Gamma Exposure (GEX) computation from options chain data."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from positionoracle.types import GEXProfile, GEXStrike

logger = logging.getLogger(__name__)


def compute_strike_range(
    spot_price: float,
    option_strikes: list[float] | None = None,
    pad: float = 0.5,
    default_pct: float = 0.05,
) -> tuple[float, float]:
    """Compute the strike range to fetch for GEX analysis.

    If option strikes are provided, pads around the min/max strikes.
    Otherwise, uses a default percentage around spot.

    Parameters
    ----------
    spot_price : float
        Current underlying price.
    option_strikes : list[float] | None
        Strikes from held option positions (if any).
    pad : float
        Padding factor applied to the distance beyond min/max strikes.
    default_pct : float
        Default percentage range when no option positions exist.

    Returns
    -------
    tuple[float, float]
        (strike_gte, strike_lte) bounds for the chain query.
    """
    if option_strikes:
        low = min(option_strikes)
        high = max(option_strikes)
        distance_below = spot_price - low
        distance_above = high - spot_price
        strike_gte = low - distance_below * pad
        strike_lte = high + distance_above * pad
        # Ensure we at least cover the default range
        strike_gte = min(strike_gte, spot_price * (1 - default_pct))
        strike_lte = max(strike_lte, spot_price * (1 + default_pct))
    else:
        strike_gte = spot_price * (1 - default_pct)
        strike_lte = spot_price * (1 + default_pct)

    return round(strike_gte, 2), round(strike_lte, 2)


def filter_chain_data(
    chain_data: list[dict[str, Any]],
    strike_gte: float,
    strike_lte: float,
) -> list[dict[str, Any]]:
    """Filter chain data to contracts within the strike range.

    Parameters
    ----------
    chain_data : list[dict[str, Any]]
        Raw contract snapshots.
    strike_gte : float
        Minimum strike price.
    strike_lte : float
        Maximum strike price.

    Returns
    -------
    list[dict[str, Any]]
        Filtered contracts.
    """
    filtered = [
        c for c in chain_data
        if strike_gte <= c.get("details", {}).get("strike_price", 0) <= strike_lte
    ]
    logger.info(
        "Filtered chain: %d -> %d contracts (range %.0f-%.0f)",
        len(chain_data), len(filtered), strike_gte, strike_lte,
    )
    return filtered


def build_gex_profile(
    underlying: str,
    spot_price: float,
    chain_data: list[dict[str, Any]],
) -> GEXProfile:
    """Build a GEX profile from raw options chain snapshot data.

    Uses dealer model: dealers are assumed short what the market is long.
    For calls, dealers are net short (negative gamma).
    For puts, dealers are net long (positive gamma, since they bought puts
    from hedgers, but put gamma flips sign for dealers).

    GEX per contract = gamma * OI * 100 * spot_price
    Call GEX is positive (dealers short calls → must buy stock as price rises).
    Put GEX is negative (dealers short puts → must sell stock as price drops).

    Parameters
    ----------
    underlying : str
        Underlying ticker.
    spot_price : float
        Current underlying price.
    chain_data : list[dict[str, Any]]
        Raw contract snapshots from Massive API.

    Returns
    -------
    GEXProfile
        Complete GEX profile with walls and flip point.
    """
    # Aggregate by strike
    strike_calls: dict[float, float] = defaultdict(float)
    strike_puts: dict[float, float] = defaultdict(float)
    strike_call_oi: dict[float, int] = defaultdict(int)
    strike_put_oi: dict[float, int] = defaultdict(int)
    expirations: set[str] = set()

    for contract in chain_data:
        details = contract.get("details", {})
        greeks = contract.get("greeks", {})
        oi = contract.get("open_interest", 0) or 0
        gamma = greeks.get("gamma", 0) or 0
        strike = details.get("strike_price", 0)
        contract_type = details.get("contract_type", "").lower()
        exp = details.get("expiration_date", "")

        if not strike or not gamma or not oi:
            continue

        expirations.add(exp)
        # GEX = gamma * OI * multiplier(100) * spot
        gex = gamma * oi * 100 * spot_price

        if contract_type == "call":
            strike_calls[strike] += gex
            strike_call_oi[strike] += oi
        elif contract_type == "put":
            # Put GEX is negative in dealer model
            strike_puts[strike] -= gex
            strike_put_oi[strike] += oi

    # Build sorted strike list
    all_strikes = sorted(set(strike_calls) | set(strike_puts))

    gex_strikes: list[GEXStrike] = []
    for s in all_strikes:
        call_gex = strike_calls.get(s, 0.0)
        put_gex = strike_puts.get(s, 0.0)
        gex_strikes.append(GEXStrike(
            strike=s,
            call_gex=call_gex,
            put_gex=put_gex,
            net_gex=call_gex + put_gex,
            call_oi=strike_call_oi.get(s, 0),
            put_oi=strike_put_oi.get(s, 0),
        ))

    # Total net GEX
    net_gex = sum(gs.net_gex for gs in gex_strikes)

    # Call Wall: strike with highest call GEX
    call_wall = 0.0
    if gex_strikes:
        call_wall = max(gex_strikes, key=lambda gs: gs.call_gex).strike

    # Put Wall: strike with most negative put GEX (strongest support)
    put_wall = 0.0
    if gex_strikes:
        put_wall = min(gex_strikes, key=lambda gs: gs.put_gex).strike

    # Flip point: strike where cumulative GEX crosses zero (scanning low to high)
    flip_point = 0.0
    if gex_strikes:
        cumulative = 0.0
        for gs in gex_strikes:
            prev = cumulative
            cumulative += gs.net_gex
            if prev <= 0 < cumulative or prev >= 0 > cumulative:
                # Linear interpolation
                if gs.net_gex != 0:
                    flip_point = gs.strike
                break
        # If no crossover found, use spot as default
        if flip_point == 0.0:
            flip_point = spot_price

    sorted_exps = sorted(expirations)

    logger.info(
        "GEX %s: %d strikes, net=%.0f, call_wall=%.0f, put_wall=%.0f, flip=%.0f",
        underlying, len(gex_strikes), net_gex, call_wall, put_wall, flip_point,
    )

    return GEXProfile(
        underlying=underlying,
        spot_price=spot_price,
        strikes=gex_strikes,
        net_gex=net_gex,
        call_wall=call_wall,
        put_wall=put_wall,
        flip_point=flip_point,
        expirations=sorted_exps,
        fetched_at=datetime.now(tz=UTC).isoformat(),
    )
