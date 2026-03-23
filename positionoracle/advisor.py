"""Position adjustment advisor based on Greek thresholds."""

from __future__ import annotations

import datetime

from positionoracle.types import (
    Advice,
    AdviceLevel,
    ContractType,
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


def build_portfolio_summary(
    position_greeks: list[PositionGreeks],
    thresholds: dict[str, float],
) -> dict[str, PortfolioSummary]:
    """Aggregate positions by underlying and generate advice.

    Parameters
    ----------
    position_greeks : list[PositionGreeks]
        All positions with their current Greeks.
    thresholds : dict[str, float]
        Advisor threshold settings.

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

    return summaries
