"""MCP server for PositionOracle, mounted at ``/mcp``.

Exposes a focused set of tools that wrap the same data the public REST
v1 API exposes, so MCP and HTTP share a single code path. Authentication
is identical to the REST v1 API — clients send an ``Authorization:
Bearer <api-key>`` header on every call. A pure-ASGI middleware
validates the key before the request reaches FastMCP.

PositionOracle is a single-user app: the API key uniquely identifies
the (one) user, so unlike multi-tenant MCP servers we don't need to
propagate a ``user_id`` through a ``ContextVar``.

Mount this once from ``main.py``::

    app.mount("/mcp", mcp_server.build_asgi_app())

…and run the session manager from the FastAPI lifespan so the MCP
transport's background tasks get cleanly torn down on shutdown::

    async with mcp_server.mcp.session_manager.run():
        yield
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from positionoracle import api_keys as api_keys_mod
from positionoracle import db, oauth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level data_dir + issuer, wired in from the FastAPI lifespan
# ---------------------------------------------------------------------------

_data_dir: Path | None = None
_issuer_origin: str = ""


def set_data_dir(data_dir: Path) -> None:
    """Wire the app's data directory in from the FastAPI lifespan."""
    global _data_dir
    _data_dir = data_dir


def set_issuer(origin: str) -> None:
    """Wire the public-facing origin so the 401 can advertise resource_metadata."""
    global _issuer_origin
    _issuer_origin = origin.rstrip("/")


def _require_data_dir() -> Path:
    if _data_dir is None:
        raise RuntimeError("MCP data_dir not initialised — set_data_dir was not called")
    return _data_dir


# ---------------------------------------------------------------------------
# The FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "positionoracle",
    instructions=(
        "Tools for PositionOracle, an options-position monitor. "
        "Start with list_positions for a compact roster of every open "
        "position (cheap), then get_position(symbol) for full Greeks / "
        "VRP / P&L on a row that matters. Use get_positions only when "
        "you need the per-underlying rollup and net Greeks across "
        "positions. get_washsale_blacklist returns the IRS 30-day "
        "wash-sale list; get_gex_profiles returns dealer gamma walls "
        "(filter by underlying — the strike grids are heavy). "
        "create_position records an intraday trade before the IB Flex "
        "sync sees it; close_position removes one by symbol."
    ),
    stateless_http=True,
    streamable_http_path="/",
    # Bearer auth on every request makes DNS-rebinding protection
    # redundant (browsers don't auto-send Authorization headers
    # cross-origin) and the default 127.0.0.1-only allow-list breaks
    # the app behind any reverse proxy.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ---------------------------------------------------------------------------
# Bearer auth — pure ASGI so it works regardless of how FastMCP exposes
# the underlying request object.
# ---------------------------------------------------------------------------


def _www_authenticate_header() -> bytes:
    """Build the ``WWW-Authenticate`` value.

    Per RFC 9728 §5.1, including ``resource_metadata`` lets MCP clients
    auto-discover the authorization server without out-of-band setup —
    this is what Claude Cowork follows on its first 401.
    """
    parts = ['Bearer realm="positionoracle"']
    if _issuer_origin:
        meta_url = f"{_issuer_origin}/.well-known/oauth-protected-resource"
        parts.append(f'resource_metadata="{meta_url}"')
    return ", ".join(parts).encode("latin-1")


async def _send_401(send: Any, message: str = "missing or invalid token") -> None:
    body = b'{"error":"' + message.encode("utf-8") + b'"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", _www_authenticate_header()),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI middleware: 401 unless a valid token is presented."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        auth = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v.decode("latin-1")
                break

        if not auth.lower().startswith("bearer "):
            await _send_401(send)
            return

        token = auth[7:].strip()
        if not token:
            await _send_401(send)
            return

        # Two valid token shapes:
        #   1. OAuth access token (opaque, hashed and looked up in
        #      oauth_tokens). This is what Claude Cowork + any DCR'd
        #      client + any /oauth/token-issued credential will send.
        #   2. Legacy static API key (po_*) — still issued from the UI
        #      so anyone who prefers a simple static Bearer can keep
        #      using one.
        try:
            grant = await db.lookup_access_token(
                _require_data_dir(), oauth.hash_token(token),
            )
        except Exception:
            logger.exception("MCP OAuth token lookup failed")
            await _send_401(send, "auth lookup failed")
            return

        if grant is not None:
            await db.touch_oauth_client(_require_data_dir(), grant.client_id)
            await self.app(scope, receive, send)
            return

        try:
            digest = api_keys_mod.hash_key(token)
            record = await db.lookup_api_key_by_hash(_require_data_dir(), digest)
        except Exception:
            logger.exception("MCP API key lookup failed")
            await _send_401(send, "auth lookup failed")
            return

        if record is None:
            await _send_401(send)
            return

        await db.touch_api_key(_require_data_dir(), record.id)
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _parse_iso_datetime(s: str) -> datetime.datetime:
    """Parse an ISO-8601 timestamp; reject malformed input."""
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"expected ISO-8601 datetime, got {s!r}") from exc


def _parse_iso_date(s: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"expected YYYY-MM-DD, got {s!r}") from exc


@mcp.tool()
async def list_positions() -> dict[str, Any]:
    """Return a compact list of every open position (one-liner per row).

    Use this first to see what's open; then call ``get_position(symbol)``
    for full Greeks / VRP / P&L on any specific row. Much cheaper than
    ``get_positions`` — the per-position payload is ~6 fields rather
    than the full ~20.

    Each row contains: ``symbol``, ``underlying``, ``contract_type``
    (``call`` / ``put`` / ``stock``), ``strike``, ``expiration`` (ISO
    date), ``quantity`` (signed; negative = short), and ``pnl_pct``
    (direction-aware fraction of entry premium; ``null`` if entry data
    isn't loaded yet). Rows are sorted underlying → expiration → strike
    to match how the UI lists them.
    """
    from positionoracle import main as app_main

    rows = []
    for pg in sorted(
        app_main._position_greeks.values(),
        key=lambda p: (p.position.underlying, p.position.expiration, p.position.strike),
    ):
        pos = pg.position
        rows.append({
            "symbol": pos.symbol,
            "underlying": pos.underlying,
            "contract_type": pos.contract_type.value,
            "strike": pos.strike,
            "expiration": pos.expiration.isoformat(),
            "quantity": pos.quantity,
            "pnl_pct": pg.pnl_pct,
        })
    return {"positions": rows, "count": len(rows)}


@mcp.tool()
async def get_position(symbol: str) -> dict[str, Any]:
    """Return full detail for one open position.

    The symbol is the IB OCC-style option string (e.g.
    ``"AAPL  251219C00150000"``) for options, or the bare ticker for
    stock — exactly as ``list_positions`` returns it. Raises a ValueError
    if no open position matches.

    Fields: position descriptors (``symbol``, ``underlying``,
    ``contract_type``, ``strike``, ``expiration``, ``quantity``,
    ``cost_basis``, ``multiplier``), live market (``underlying_price``,
    ``option_mid``, ``theoretical_mid``), P&L (``pnl_pct``), full
    ``greeks`` (delta/gamma/theta/vega + second-order vanna/charm/vomma
    + ``implied_volatility``), and VRP context (``vrp``, ``entry_iv``,
    ``rv``, ``rv_window_days``).
    """
    import dataclasses

    from positionoracle import main as app_main

    pg = app_main._position_greeks.get(symbol)
    if pg is None:
        raise ValueError(f"no open position with symbol {symbol!r}")
    pos = pg.position
    return {
        "symbol": pos.symbol,
        "underlying": pos.underlying,
        "contract_type": pos.contract_type.value,
        "strike": pos.strike,
        "expiration": pos.expiration.isoformat(),
        "quantity": pos.quantity,
        "cost_basis": pos.cost_basis,
        "multiplier": pos.multiplier,
        "underlying_price": pg.underlying_price,
        "option_mid": pg.option_mid,
        "theoretical_mid": pg.theoretical_mid,
        "pnl_pct": pg.pnl_pct,
        "greeks": dataclasses.asdict(pg.greeks),
        "vrp": pg.vrp,
        "entry_iv": pg.entry_iv,
        "rv": pg.rv,
        "rv_window_days": pg.rv_window_days,
    }


@mcp.tool()
async def get_positions(include_gex: bool = False) -> dict[str, Any]:
    """Return every open position grouped by underlying, with rollup.

    For most use cases ``list_positions`` is the cheaper starting point
    — this tool returns every per-position field plus per-underlying
    net Greeks, beta-weighted delta, and advice items. Prefer it when
    you actually need the cross-position view (e.g. "show me net theta
    on SPY").

    Top-level keys:

    - ``last_updated`` — ISO timestamp of when this snapshot was rendered.
    - ``last_report_generated`` — ISO timestamp of the most recent IB
      Flex report, or null if no import has happened.
    - ``market_open`` — true if US equity regular session is live.
    - ``underlyings`` — map of ticker → net Greeks, beta-weighted delta,
      per-position detail, and advice items.
    - ``portfolio`` — rollup across all underlyings.
    - ``gex`` — per-underlying gamma exposure profiles. **Omitted by
      default to keep the payload small.** Pass ``include_gex=True`` to
      embed the full strike grid here, or call ``get_gex_profiles`` for
      a single ticker.
    """
    from positionoracle import main as app_main
    from positionoracle.advisor import build_portfolio_summary

    thresholds = await db.get_thresholds(_require_data_dir())
    all_pgs = list(app_main._position_greeks.values())
    summaries = build_portfolio_summary(all_pgs, thresholds, app_main._gex_profiles)
    payload = app_main._serialize_summaries(summaries)
    if not include_gex:
        payload.pop("gex", None)
    return payload


@mcp.tool()
async def get_washsale_blacklist() -> dict[str, Any]:
    """Return symbols inside their IRS 30-day wash-sale window.

    Buying any returned ``symbol`` before its ``expires`` date triggers
    a wash sale on a previously realized loss. Each entry includes
    ``days_remaining`` (ET-based) so the caller can prioritize.
    """
    from positionoracle import main as app_main

    await app_main._reload_blacklist()
    today_et = datetime.datetime.now(tz=app_main._ET).date()
    return {
        "entries": [
            {
                "symbol": e.symbol,
                "loss_date": e.loss_date.isoformat(),
                "expires": e.expires.isoformat(),
                "days_remaining": max(0, (e.expires - today_et).days),
            }
            for e in app_main._blacklist
        ],
        "last_report_generated": app_main._last_report_generated,
    }


@mcp.tool()
async def get_gex_profiles(underlying: str | None = None) -> dict[str, Any]:
    """Return dealer gamma exposure profiles.

    Parameters
    ----------
    underlying : str | None
        Filter to one ticker (e.g. ``"SPY"``). When omitted, returns
        every cached profile.

    Each profile reports the spot price, net GEX, call/put walls, the
    zero-gamma flip point, the expirations included, and per-strike GEX
    detail. Returns an empty ``profiles`` map when no GEX data has been
    fetched yet.
    """
    from positionoracle import main as app_main

    profiles = app_main._gex_profiles
    if underlying:
        key = underlying.upper()
        profiles = {key: profiles[key]} if key in profiles else {}

    return {
        "profiles": {
            ticker: {
                "underlying": p.underlying,
                "spot_price": p.spot_price,
                "net_gex": p.net_gex,
                "call_wall": p.call_wall,
                "put_wall": p.put_wall,
                "flip_point": p.flip_point,
                "expirations": p.expirations,
                "fetched_at": p.fetched_at,
                "strikes": [
                    {
                        "strike": gs.strike,
                        "call_gex": gs.call_gex,
                        "put_gex": gs.put_gex,
                        "net_gex": gs.net_gex,
                        "call_oi": gs.call_oi,
                        "put_oi": gs.put_oi,
                    }
                    for gs in p.strikes
                ],
            }
            for ticker, p in profiles.items()
        },
    }


@mcp.tool()
async def create_position(
    underlying: str,
    contract_type: str,
    quantity: int,
    entry_time: str,
    entry_premium_per_share: float,
    strike: float | None = None,
    expiration: str | None = None,
    multiplier: int | None = None,
) -> dict[str, Any]:
    """Record or update an intraday position before IB's next Flex sync.

    Upserts by symbol: submitting a position whose symbol already exists
    replaces it in place, so this is also how you change a position's
    quantity (e.g. scaling 100 shares to 200) between Flex syncs.

    Parameters
    ----------
    underlying : str
        Underlying ticker, e.g. ``"AAPL"``.
    contract_type : str
        ``"call"``, ``"put"``, or ``"stock"``.
    quantity : int
        Signed contract count. Negative = short.
    entry_time : str
        ISO-8601 timestamp of the opening trade. Tz-aware preferred;
        naive timestamps are assumed to be ET.
    entry_premium_per_share : float
        Per-share premium paid (long) or received (short). Always
        positive.
    strike : float | None
        Required for option contracts; ignored for stock.
    expiration : str | None
        ISO date (``YYYY-MM-DD``). Required for options; ignored for stock.
    multiplier : int | None
        Contract multiplier. Defaults to 100 for options, 1 for stock.

    The server synchronously fetches the 1-min entry spot from Massive,
    the entry risk-free rate from FRED, and back-solves entry IV so VRP
    and P&L% populate before this call returns.

    **Reconciliation warning:** the next IB Flex sync upserts by symbol.
    If IB has the trade, the manual entry is updated in place. If IB
    hasn't booked it yet, the sync's cleanup step deletes the manual
    entry. Trigger a sync only after IB has caught up.
    """
    from fastapi import HTTPException

    from positionoracle import main as app_main
    from positionoracle.api_models import CreatePositionRequest

    body = CreatePositionRequest(
        underlying=underlying,
        contract_type=contract_type,
        quantity=quantity,
        entry_time=_parse_iso_datetime(entry_time),
        entry_premium_per_share=entry_premium_per_share,
        strike=strike,
        expiration=_parse_iso_date(expiration) if expiration else None,
        multiplier=multiplier,
    )

    try:
        # The FastAPI route doesn't touch ``api_key`` — it's only used
        # by the dependency to authenticate the HTTP request. We've
        # already authenticated at the MCP middleware layer.
        response = await app_main.v1_create_position(body=body)  # type: ignore[call-arg]
    except HTTPException as exc:
        raise ValueError(f"{exc.status_code}: {exc.detail}") from exc

    return response.model_dump(mode="json")


@mcp.tool()
async def price_option(
    underlying: str,
    contract_type: str,
    direction: str,
    strike: float,
    expiration: str,
) -> dict[str, Any]:
    """Price an option at VRP=1.0 to plan a trade before entering it.

    Returns the Black-Scholes price at which implied vol would equal the
    underlying's trailing realized vol (VRP == 1.0) — the fair-value
    anchor. A ``short`` should aim to collect at least this; a ``long``
    should aim to pay no more than this. When a live options chain is
    available each strike also carries the current IV, mid, VRP, and a
    direction-aware verdict, plus a few nearby strikes for comparison.

    Parameters
    ----------
    underlying : str
        Underlying ticker, e.g. ``"AAPL"``.
    contract_type : str
        ``"call"`` or ``"put"``.
    direction : str
        ``"long"`` (buying) or ``"short"`` (selling).
    strike : float
        Contract strike.
    expiration : str
        ISO date (``YYYY-MM-DD``). Must be today or later.
    """
    from fastapi import HTTPException

    from positionoracle import main as app_main
    from positionoracle.api_models import PriceOptionRequest

    body = PriceOptionRequest(
        underlying=underlying,
        contract_type=contract_type,
        direction=direction,
        strike=strike,
        expiration=_parse_iso_date(expiration),
    )

    try:
        response = await app_main._compute_price_plan(body)
    except HTTPException as exc:
        raise ValueError(f"{exc.status_code}: {exc.detail}") from exc

    return response.model_dump(mode="json")


@mcp.tool()
async def close_position(symbol: str) -> dict[str, str]:
    """Close (delete) a position by its exact symbol.

    For options, ``symbol`` is the IB OCC-style string (e.g.
    ``"AAPL  251219C00150000"``). For stock, it's the bare ticker.
    """
    from fastapi import HTTPException

    from positionoracle import main as app_main

    try:
        await app_main.v1_delete_position(symbol=symbol)  # type: ignore[call-arg]
    except HTTPException as exc:
        raise ValueError(f"{exc.status_code}: {exc.detail}") from exc

    return {"status": "ok", "symbol": symbol}


# ---------------------------------------------------------------------------
# ASGI app builder
# ---------------------------------------------------------------------------


def build_asgi_app() -> Any:
    """Return the auth-wrapped Streamable-HTTP ASGI app to mount."""
    return BearerAuthMiddleware(mcp.streamable_http_app())
