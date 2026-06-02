"""Claude-powered position analysis for actionable trading advice."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert options trading advisor. You analyze options positions \
using their Greeks plus two anchor metrics — VRP and P&L% — and provide concise, actionable \
advice.

Your audience is an experienced retail options trader who sells premium (short puts, short \
strangles, covered calls). They understand Greeks but want your interpretation of what the \
current values mean for their specific positions and whether action is needed.

Two metrics are central to close/roll decisions:

- **P&L %**: direction-aware profit/loss as a fraction of entry premium, using a \
Black-Scholes theoretical mid from live IV. For shorts, positive = premium has decayed in \
the writer's favor. >=80% is candidate-to-close territory ("most of the max profit is in, \
the remaining tail risk no longer pays"). Negative = position is underwater vs entry.

- **VRP**: realized vol over the trailing 21 trading days divided by the IV implied by \
the option's entry premium. <1.0 means the underlying is realizing *less* vol than the \
writer was paid for (thesis held). >1.0 means realized exceeds entry IV (thesis broke). \
For longs the interpretation flips.

For each analysis, consider:
1. Is the position working as intended? Use P&L% (premium earned), VRP (thesis intact?), \
   and theta (still collecting?).
2. Are any Greeks signaling danger? (delta approaching ITM, gamma risk near expiry, \
   vanna exposure before events.)
3. Is there meaningfully more to earn vs more to lose? High P&L% (>=70%) with weeks of \
   DTE left often means "picking up nickels" — the remaining premium is small relative to \
   tail risk.
4. What does the GEX landscape tell us? (positive/negative regime, proximity to walls \
   and flip point, how dealer hedging flows affect position risk.)
5. What specific action? (hold, roll, close, adjust.) And what time horizon? (urgent now, \
   watch this week, monitor.)

Be direct and specific. No generic disclaimers. Use the actual numbers provided."""


def _format_position_context(
    underlying: str,
    summary: dict[str, Any],
    spot_price: float,
    beta: float,
    beta_weighted_delta: float,
    gex_data: dict[str, Any] | None = None,
) -> str:
    """Build a context string for Claude from position data.

    Parameters
    ----------
    underlying : str
        Ticker symbol.
    summary : dict[str, Any]
        Serialized portfolio summary for this underlying.
    spot_price : float
        Current underlying price.
    beta : float
        Beta vs SPY.
    beta_weighted_delta : float
        SPY-equivalent delta.
    gex_data : dict[str, Any] | None
        Serialized GEX profile for this underlying.

    Returns
    -------
    str
        Formatted context for the prompt.
    """
    lines = [
        f"## {underlying} — Spot: ${spot_price:.2f} | Beta: {beta:.2f}",
        "",
        "### Aggregate Exposure",
        f"- Net Delta: {summary['net_delta']:.2f} "
        f"(SPY-equivalent: {beta_weighted_delta:.1f} shares)",
        f"- Net Gamma: {summary['net_gamma']:.2f}",
        f"- Net Theta: {summary['net_theta']:.2f}/day",
        f"- Net Vega: {summary['net_vega']:.2f}",
        "",
    ]

    if gex_data:
        net_gex = gex_data.get("net_gex", 0)
        regime = "POSITIVE (dampening)" if net_gex > 0 else "NEGATIVE (amplifying)"
        lines.extend([
            "### GEX Landscape",
            f"- Net GEX: {net_gex:,.0f} ({regime})",
            f"- Call Wall (resistance): ${gex_data.get('call_wall', 0):.0f}",
            f"- Put Wall (support): ${gex_data.get('put_wall', 0):.0f}",
            f"- Flip Point: ${gex_data.get('flip_point', 0):.0f}",
            f"- Data as of: {gex_data.get('fetched_at', 'unknown')}",
            "",
        ])

    lines.append("### Positions")

    for pos in summary.get("positions", []):
        g = pos.get("greeks", {})
        ct = pos["contract_type"].upper()
        if ct == "STOCK":
            lines.append(
                f"- {ct}: {pos['quantity']} shares "
                f"(delta contribution: {pos['quantity']})"
            )
        else:
            dte_str = pos.get("expiration", "?")
            iv_pct = g.get("implied_volatility", 0) * 100
            pnl_str = _fmt_pnl_pct(pos.get("pnl_pct"))
            vrp_str = _fmt_vrp(
                pos.get("vrp"),
                pos.get("entry_iv"),
                pos.get("rv"),
                pos.get("rv_window_days", 0),
            )
            lines.append(
                f"- {ct} {pos['strike']:.0f} exp {dte_str} "
                f"qty {pos['quantity']}"
            )
            lines.append(f"    P&L {pnl_str} | VRP {vrp_str}")
            lines.append(
                f"    IV {iv_pct:.1f}% | "
                f"Δ {g.get('delta', 0):.4f} "
                f"Θ {g.get('theta', 0):.4f}/day "
                f"V {g.get('vega', 0):.4f} "
                f"Γ {g.get('gamma', 0):.4f}"
            )
            lines.append(
                f"    Vanna {g.get('vanna', 0):.4f} "
                f"Charm {g.get('charm', 0):.4f}Δ/day "
                f"Vomma {g.get('vomma', 0):.4f}"
            )

    return "\n".join(lines)


def _fmt_pnl_pct(pnl_pct: float | None) -> str:
    """Format P&L% for the Claude context block."""
    if pnl_pct is None:
        return "—"
    return f"{pnl_pct * 100:+.0f}%"


def _fmt_vrp(
    vrp: float | None,
    entry_iv: float | None,
    rv: float | None,
    rv_window_days: int,
) -> str:
    """Format VRP with supporting context for the Claude prompt.

    Examples
    --------
    - ``0.82 (entry IV 30.0% vs 21d RV 24.5%)`` — fully populated
    - ``— (entry IV pending)`` — entry not yet backfilled
    """
    if vrp is None:
        if entry_iv is None:
            return "— (entry IV pending)"
        return "— (insufficient realized-vol data)"
    iv_pct = entry_iv * 100 if entry_iv else 0.0
    rv_pct = rv * 100 if rv else 0.0
    return (
        f"{vrp:.2f} (entry IV {iv_pct:.1f}% vs {rv_window_days}d RV {rv_pct:.1f}%)"
    )


async def analyze_symbol(
    api_key: str,
    model: str,
    underlying: str,
    summary: dict[str, Any],
    spot_price: float,
    beta: float,
    beta_weighted_delta: float,
    gex_data: dict[str, Any] | None = None,
) -> str:
    """Get Claude's analysis of a symbol's positions.

    Parameters
    ----------
    api_key : str
        Anthropic API key.
    model : str
        Claude model ID (e.g. ``"claude-sonnet-4-6"``).
    underlying : str
        Ticker symbol.
    summary : dict[str, Any]
        Serialized portfolio summary for this underlying.
    spot_price : float
        Current underlying price.
    beta : float
        Beta vs SPY.
    beta_weighted_delta : float
        SPY-equivalent delta.
    gex_data : dict[str, Any] | None
        Serialized GEX profile for this underlying.

    Returns
    -------
    str
        Claude's analysis as markdown text.
    """
    context = _format_position_context(
        underlying, summary, spot_price, beta, beta_weighted_delta, gex_data,
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)

    logger.info("Requesting analysis for %s using %s", underlying, model)

    message = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today's date is {datetime.now(tz=UTC).strftime('%B %d, %Y')}.\n\n"
                    f"Analyze my {underlying} position and tell me what to do:\n\n"
                    f"{context}"
                ),
            }
        ],
    )

    return message.content[0].text
