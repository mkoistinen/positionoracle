"""Claude-powered position analysis for actionable trading advice."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert options trading advisor. You analyze options positions \
using their Greeks and provide concise, actionable advice.

Your audience is an experienced retail options trader who sells premium (short puts, short \
strangles, covered calls). They understand Greeks but want your interpretation of what the \
current values mean for their specific positions and whether action is needed.

For each analysis, consider:
1. Is the position working as intended? (time decay collecting, staying OTM)
2. Are any Greeks signaling danger? (delta approaching ITM, gamma risk near expiry, \
   vanna exposure before events)
3. What does the GEX landscape tell us? (positive/negative regime, proximity to walls \
   and flip point, how dealer hedging flows affect position risk)
4. What specific action, if any, should be taken? (hold, roll, close, adjust)
5. What is the time horizon for action? (urgent now, watch this week, monitor)

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
            lines.append(
                f"- {ct} {pos['strike']:.0f} exp {dte_str} "
                f"qty {pos['quantity']} | "
                f"IV {iv_pct:.1f}% | "
                f"Δ {g.get('delta', 0):.4f} "
                f"Θ {g.get('theta', 0):.4f}/day "
                f"V {g.get('vega', 0):.4f} "
                f"Γ {g.get('gamma', 0):.4f} | "
                f"Vanna {g.get('vanna', 0):.4f} "
                f"Charm {g.get('charm', 0):.4f}Δ/day "
                f"Vomma {g.get('vomma', 0):.4f}"
            )

    return "\n".join(lines)


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
