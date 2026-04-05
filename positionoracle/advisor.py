"""Position adjustment advisor based on Greek thresholds."""

from __future__ import annotations

import datetime

from positionoracle.types import (
    Advice,
    AdviceLevel,
    ContractType,
    GEXProfile,
    PortfolioSummary,
    PositionGreeks,
)


def evaluate_position(
    pg: PositionGreeks,
    thresholds: dict[str, float],
) -> list[Advice]:
    """Generate advice for a single position based on its Greeks.

    Parameters
    ----------
    pg : PositionGreeks
        Position with current Greeks.
    thresholds : dict[str, float]
        Threshold values for each metric.

    Returns
    -------
    list[Advice]
        List of advice items (may be empty if no thresholds breached).
    """
    advice: list[Advice] = []
    pos = pg.position
    g = pg.greeks

    # Stock positions don't need Greek-based advice
    if pos.contract_type == ContractType.STOCK:
        return advice

    # Days to expiration
    dte = (pos.expiration - datetime.date.today()).days

    # Delta exposure
    delta_warn = thresholds.get("delta_warn", 0.30)
    delta_urgent = thresholds.get("delta_urgent", 0.50)

    if abs(g.delta) >= delta_urgent:
        advice.append(Advice(
            level=AdviceLevel.URGENT,
            message=(
                f"Delta is {g.delta:.3f} — position is heavily directional. "
                f"Consider adjusting or hedging."
            ),
            position_symbol=pos.symbol,
            metric="delta",
            value=g.delta,
            threshold=delta_urgent,
        ))
    elif abs(g.delta) >= delta_warn:
        advice.append(Advice(
            level=AdviceLevel.WARNING,
            message=(
                f"Delta is {g.delta:.3f} — approaching directional risk threshold."
            ),
            position_symbol=pos.symbol,
            metric="delta",
            value=g.delta,
            threshold=delta_warn,
        ))

    # Gamma risk near expiration
    dte_gamma_warn = int(thresholds.get("dte_gamma_warn", 7))
    gamma_warn = thresholds.get("gamma_warn", 0.10)

    if dte <= dte_gamma_warn and abs(g.gamma) >= gamma_warn:
        advice.append(Advice(
            level=AdviceLevel.URGENT,
            message=(
                f"High gamma ({g.gamma:.4f}) with only {dte} DTE — "
                f"delta will swing rapidly. Consider closing or rolling."
            ),
            position_symbol=pos.symbol,
            metric="gamma",
            value=g.gamma,
            threshold=gamma_warn,
        ))

    # Theta decay
    theta_warn = thresholds.get("theta_warn", -0.05)
    if pos.quantity > 0 and g.theta < theta_warn:
        advice.append(Advice(
            level=AdviceLevel.WARNING,
            message=(
                f"Theta decay is {g.theta:.4f}/day — "
                f"time is eroding this long position."
            ),
            position_symbol=pos.symbol,
            metric="theta",
            value=g.theta,
            threshold=theta_warn,
        ))

    # Vega exposure
    vega_warn = thresholds.get("vega_warn", 0.20)
    if abs(g.vega) >= vega_warn:
        advice.append(Advice(
            level=AdviceLevel.WARNING,
            message=(
                f"Vega is {g.vega:.4f} — significant volatility exposure."
            ),
            position_symbol=pos.symbol,
            metric="vega",
            value=g.vega,
            threshold=vega_warn,
        ))

    # Charm warning — delta shifting fast with time
    if abs(g.charm) > 0.01:
        advice.append(Advice(
            level=AdviceLevel.INFO,
            message=(
                f"Charm is {g.charm:.4f}\u0394/day — delta is shifting "
                f"{'toward' if g.charm > 0 else 'away from'} zero with time."
            ),
            position_symbol=pos.symbol,
            metric="charm",
            value=g.charm,
            threshold=0.01,
        ))

    return advice


def evaluate_gex(
    underlying: str,
    gex_profile: GEXProfile,
    positions: list[PositionGreeks],
) -> list[Advice]:
    """Generate advice based on GEX profile relative to held positions.

    Parameters
    ----------
    underlying : str
        Underlying ticker.
    gex_profile : GEXProfile
        Current GEX profile for this underlying.
    positions : list[PositionGreeks]
        Positions in this underlying.

    Returns
    -------
    list[Advice]
        GEX-related advice items.
    """
    advice: list[Advice] = []
    spot = gex_profile.spot_price

    if not spot or not gex_profile.strikes:
        return advice

    # Negative GEX regime — volatility amplification
    if gex_profile.net_gex < 0:
        advice.append(Advice(
            level=AdviceLevel.WARNING,
            message=(
                f"Negative GEX regime (net {gex_profile.net_gex:,.0f}) — "
                f"dealer hedging will amplify moves. Elevated volatility expected."
            ),
            position_symbol=underlying,
            metric="gex_regime",
            value=gex_profile.net_gex,
            threshold=0,
        ))

    # Spot near Put Wall — support level
    if gex_profile.put_wall:
        put_wall_dist = (spot - gex_profile.put_wall) / spot
        if 0 < put_wall_dist <= 0.02:
            advice.append(Advice(
                level=AdviceLevel.INFO,
                message=(
                    f"Spot ${spot:.2f} is {put_wall_dist:.1%} above "
                    f"Put Wall at ${gex_profile.put_wall:.0f} — "
                    f"dealer hedging provides support here."
                ),
                position_symbol=underlying,
                metric="gex_put_wall",
                value=spot,
                threshold=gex_profile.put_wall,
            ))
        elif put_wall_dist <= 0:
            advice.append(Advice(
                level=AdviceLevel.WARNING,
                message=(
                    f"Spot ${spot:.2f} has breached Put Wall at "
                    f"${gex_profile.put_wall:.0f} — support lost, "
                    f"expect accelerated selling from dealer hedging."
                ),
                position_symbol=underlying,
                metric="gex_put_wall",
                value=spot,
                threshold=gex_profile.put_wall,
            ))

    # Spot near Call Wall — resistance level
    if gex_profile.call_wall:
        call_wall_dist = (gex_profile.call_wall - spot) / spot
        if 0 < call_wall_dist <= 0.02:
            advice.append(Advice(
                level=AdviceLevel.INFO,
                message=(
                    f"Spot ${spot:.2f} is {call_wall_dist:.1%} below "
                    f"Call Wall at ${gex_profile.call_wall:.0f} — "
                    f"expect resistance from dealer hedging."
                ),
                position_symbol=underlying,
                metric="gex_call_wall",
                value=spot,
                threshold=gex_profile.call_wall,
            ))

    # Check if any held strikes are near the flip point
    if gex_profile.flip_point:
        for pg in positions:
            pos = pg.position
            if pos.contract_type == ContractType.STOCK:
                continue
            flip_dist = abs(pos.strike - gex_profile.flip_point) / spot
            if flip_dist <= 0.02:
                advice.append(Advice(
                    level=AdviceLevel.WARNING,
                    message=(
                        f"{pos.contract_type.value.upper()} {pos.strike:.0f} "
                        f"is near the GEX flip point at "
                        f"${gex_profile.flip_point:.0f} — volatility regime "
                        f"change zone. Delta may become unstable."
                    ),
                    position_symbol=pos.symbol,
                    metric="gex_flip",
                    value=pos.strike,
                    threshold=gex_profile.flip_point,
                ))

    return advice


def build_portfolio_summary(
    position_greeks: list[PositionGreeks],
    thresholds: dict[str, float],
    gex_profiles: dict[str, GEXProfile] | None = None,
) -> dict[str, PortfolioSummary]:
    """Aggregate positions by underlying and generate advice.

    Parameters
    ----------
    position_greeks : list[PositionGreeks]
        All positions with their current Greeks.
    thresholds : dict[str, float]
        Advisor threshold settings.
    gex_profiles : dict[str, GEXProfile] | None
        GEX profiles keyed by underlying ticker.

    Returns
    -------
    dict[str, PortfolioSummary]
        Per-underlying portfolio summaries keyed by ticker.
    """
    summaries: dict[str, PortfolioSummary] = {}

    for pg in position_greeks:
        underlying = pg.position.underlying
        if underlying not in summaries:
            summaries[underlying] = PortfolioSummary(underlying=underlying)

        summary = summaries[underlying]
        qty = pg.position.quantity
        mult = pg.position.multiplier

        summary.net_delta += pg.greeks.delta * qty * mult
        summary.net_gamma += pg.greeks.gamma * qty * mult
        summary.net_theta += pg.greeks.theta * qty * mult
        summary.net_vega += pg.greeks.vega * qty * mult
        summary.positions.append(pg)
        summary.advice.extend(evaluate_position(pg, thresholds))

    # Add GEX-based advice
    if gex_profiles:
        for underlying, summary in summaries.items():
            gex_profile = gex_profiles.get(underlying)
            if gex_profile:
                summary.advice.extend(
                    evaluate_gex(underlying, gex_profile, summary.positions),
                )

    return summaries
