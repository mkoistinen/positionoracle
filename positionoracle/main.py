"""FastAPI application for PositionOracle."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from positionoracle import (
    api_keys as api_keys_mod,
)
from positionoracle import (
    auth,
    beta,
    db,
    flex,
    fred,
    gex,
    massive,
    vrp,
)
from positionoracle.advisor import build_portfolio_summary
from positionoracle.api_models import (
    ApiKeyCreated,
    ApiKeyList,
    ApiKeyListItem,
    CreateApiKeyRequest,
    CreatedPositionResponse,
    CreatePositionRequest,
    PositionsResponse,
    WashsaleResponse,
)
from positionoracle.config import Settings, get_settings
from positionoracle.greeks import compute_greeks_from_massive
from positionoracle.types import (
    ApiKey,
    BlacklistEntry,
    ContractType,
    GEXProfile,
    Greeks,
    OpeningTrade,
    Position,
    PositionEntry,
    PositionGreeks,
)
from positionoracle.ws import ConnectionManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

settings: Settings = get_settings()
manager = ConnectionManager()
stock_ws: massive.StockWebSocket | None = None
http_client: httpx.AsyncClient | None = None

# Live data caches
_underlying_prices: dict[str, float] = {}
_position_greeks: dict[str, PositionGreeks] = {}
_positions: list[Position] = []
_blacklist: list[BlacklistEntry] = []
_position_entries: dict[str, PositionEntry] = {}
# Recent daily closes per underlying for VRP realized-vol computation.
# Refreshed alongside the snapshot loop. Closes are oldest-first.
_underlying_closes: dict[str, list[float]] = {}
# ISO date of the most recent refresh per underlying.
_underlying_closes_date: dict[str, str] = {}
_last_report_generated: str | None = None
_snapshot_task: asyncio.Task[None] | None = None
_beta_task: asyncio.Task[None] | None = None
_beta_data: dict[str, Any] = {}  # {"betas": {...}, "spy_price": ..., "computed_at": ...}
_gex_profiles: dict[str, GEXProfile] = {}  # keyed by underlying
_gex_task: asyncio.Task[None] | None = None
_background_tasks: set[asyncio.Task[None]] = set()

_COOKIE_NAME = "po_session"
_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days

_ET = ZoneInfo("America/New_York")
_FLEX_REPORT_DATE_KEY = "flex_last_report_generated"
_FLEX_LAST_ATTEMPT_KEY = "flex_last_fetch_attempt"
# IB publishes EOD Flex reports after market close. We assume today's
# report becomes available by 17:00 ET; before that, the previous
# business day's report is the latest expected.
_FLEX_PUBLISH_HOUR_ET = 17
# After a failed IB fetch, hold off retries for this many seconds.
_FLEX_FAILURE_BACKOFF = 30 * 60


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _signer() -> TimestampSigner:
    """Return a timestamp signer using the app secret key."""
    return TimestampSigner(settings.secret_key)


def _create_session_cookie() -> str:
    """Create a signed session cookie value."""
    return _signer().sign("authenticated").decode()


def _verify_session(cookie_value: str | None) -> bool:
    """Verify a session cookie.

    Parameters
    ----------
    cookie_value : str | None
        The raw cookie value.

    Returns
    -------
    bool
        True if the session is valid.
    """
    if not cookie_value:
        return False
    try:
        _signer().unsign(cookie_value, max_age=_COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(request: Request) -> None:
    """Raise 401 if the request is not authenticated.

    Parameters
    ----------
    request : Request
        The incoming request.

    Raises
    ------
    HTTPException
        If no valid session cookie is present.
    """
    cookie = request.cookies.get(_COOKIE_NAME)
    if not _verify_session(cookie):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Market data orchestration
# ---------------------------------------------------------------------------


async def _on_trade(ticker: str, price: float) -> None:
    """Handle a real-time trade update from Massive.

    Parameters
    ----------
    ticker : str
        Stock ticker symbol.
    price : float
        Trade price.
    """
    old = _underlying_prices.get(ticker, 0.0)
    _underlying_prices[ticker] = price
    if old != price:
        logger.debug("WS price %s: %.2f -> %.2f", ticker, old, price)
    # Recompute Greeks for positions on this underlying
    await _recompute_positions(ticker)


async def _init_position_greeks() -> None:
    """Sync the in-memory Greeks cache with the current positions list.

    Adds placeholders for new positions and removes entries for positions
    that no longer exist.
    """
    current_symbols = {pos.symbol for pos in _positions}

    # Remove stale entries
    for symbol in list(_position_greeks):
        if symbol not in current_symbols:
            del _position_greeks[symbol]

    # Add placeholders for new positions
    for pos in _positions:
        if pos.symbol not in _position_greeks:
            greeks = Greeks(delta=1.0) if pos.contract_type == ContractType.STOCK else Greeks()
            _position_greeks[pos.symbol] = PositionGreeks(
                position=pos,
                greeks=greeks,
                underlying_price=_underlying_prices.get(pos.underlying, 0.0),
            )


def _apply_derived_metrics_to_position(pg: PositionGreeks) -> None:
    """Attach VRP / entry_iv / rv / theoretical_mid / pnl_pct to a position.

    Reads from the in-memory caches ``_position_entries`` and
    ``_underlying_closes``. Fields are set to ``None`` when inputs are
    unavailable so the frontend can render a dash.

    P&L% uses the BS theoretical price (computed from live IV + spot)
    rather than quote-derived mid, because the user's Massive tier
    doesn't always return bid/ask.
    """
    pos = pg.position
    if pos.contract_type == ContractType.STOCK:
        pg.vrp = None
        pg.entry_iv = None
        pg.rv = None
        pg.rv_window_days = 0
        pg.theoretical_mid = None
        pg.pnl_pct = None
        return

    entry = _position_entries.get(pos.symbol)
    closes = _underlying_closes.get(pos.underlying, [])

    pg.entry_iv = entry.entry_iv if entry else None

    # --- Theoretical mid via Black-Scholes from live IV ---
    dte = (pos.expiration - datetime.date.today()).days
    if (
        pg.underlying_price > 0
        and pg.greeks.implied_volatility > 0
        and dte > 0
    ):
        t_years = max(dte / 365.0, 1 / 365.0)
        rate = entry.entry_rate if entry else 0.05
        pg.theoretical_mid = vrp.bs_price(
            s=pg.underlying_price,
            k=pos.strike,
            t=t_years,
            r=rate,
            sigma=pg.greeks.implied_volatility,
            contract_type=pos.contract_type,
        )
    else:
        pg.theoretical_mid = None

    # --- P&L% (direction-aware, anchored to entry premium) ---
    if (
        entry
        and entry.entry_premium_per_share > 0
        and pg.theoretical_mid is not None
    ):
        current = pg.theoretical_mid
        entry_prem = entry.entry_premium_per_share
        if pos.quantity < 0:
            pg.pnl_pct = (entry_prem - current) / entry_prem
        else:
            pg.pnl_pct = (current - entry_prem) / entry_prem
    else:
        pg.pnl_pct = None

    # --- VRP ---
    if len(closes) < 2:
        pg.rv = None
        pg.rv_window_days = 0
        pg.vrp = None
        return

    rv = vrp.realized_vol_annualized(closes, window=vrp.DEFAULT_RV_WINDOW)
    if rv != rv:  # NaN
        pg.rv = None
        pg.rv_window_days = 0
        pg.vrp = None
        return

    pg.rv = rv
    pg.rv_window_days = min(len(closes) - 1, vrp.DEFAULT_RV_WINDOW)

    if entry and entry.entry_iv:
        ratio = vrp.vrp_ratio(rv, entry.entry_iv)
        pg.vrp = None if ratio != ratio else ratio
    else:
        pg.vrp = None


async def _refresh_underlying_closes() -> None:
    """Refresh trailing daily-close caches needed for VRP.

    Pulls ~30 calendar days of daily bars per underlying so we have
    enough trading-day closes for a trailing-21 RV. Skips tickers
    already refreshed today.
    """
    global http_client

    if not settings.massive_api_key or not _positions:
        return
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=30)

    today_iso = datetime.date.today().isoformat()
    underlyings = {
        p.underlying for p in _positions if p.contract_type != ContractType.STOCK
    }
    for ticker in underlyings:
        if _underlying_closes_date.get(ticker) == today_iso:
            continue
        bars = await massive.get_daily_bars(
            settings.massive_api_key, ticker, days=30, client=http_client,
        )
        closes = [float(b["c"]) for b in bars if b.get("c")]
        if len(closes) >= 2:
            _underlying_closes[ticker] = closes
            _underlying_closes_date[ticker] = today_iso
            logger.info("VRP closes for %s: %d bars cached", ticker, len(closes))
        else:
            logger.warning("VRP closes for %s: insufficient data", ticker)


async def _recompute_positions(underlying: str | None = None) -> None:
    """Recompute Greeks and broadcast updates.

    Parameters
    ----------
    underlying : str | None
        If provided, only recompute positions for this underlying.
    """
    positions = [
        p for p in _positions
        if underlying is None or p.underlying == underlying
    ]

    for pos in positions:
        pg = _position_greeks.get(pos.symbol)
        if pg:
            if pos.underlying in _underlying_prices:
                pg.underlying_price = _underlying_prices[pos.underlying]
            _apply_derived_metrics_to_position(pg)

    if manager.has_connections:
        thresholds = await db.get_thresholds(settings.data_dir)
        all_pgs = list(_position_greeks.values())
        summaries = build_portfolio_summary(all_pgs, thresholds, _gex_profiles)
        await manager.broadcast(_serialize_summaries(summaries))


def _is_market_open() -> bool:
    """Check if the US equity market is currently open."""
    from zoneinfo import ZoneInfo

    et_now = datetime.datetime.now(tz=ZoneInfo("America/New_York"))
    if et_now.weekday() >= 5:
        return False
    minutes = et_now.hour * 60 + et_now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


async def _snapshot_loop() -> None:
    """Periodically poll Massive for options snapshots (Greeks + IV)."""
    while True:
        try:
            await _refresh_options_snapshots()
        except Exception:
            logger.exception("Error refreshing options snapshots")
        interval = 15 if _is_market_open() else 60
        await asyncio.sleep(interval)


async def _beta_loop() -> None:
    """Check and refresh betas once per day, independently of the snapshot loop."""
    global _beta_data, http_client

    while True:
        try:
            if not _positions or not settings.massive_api_key:
                await asyncio.sleep(60)
                continue

            today = datetime.date.today().isoformat()
            if _beta_data.get("computed_at") != today:
                if http_client is None:
                    http_client = httpx.AsyncClient(timeout=30)

                underlyings = {p.underlying for p in _positions}
                _beta_data = await beta.refresh_betas(
                    settings.massive_api_key,
                    underlyings,
                    settings.data_dir,
                    client=http_client,
                )
                # Broadcast updated data with betas
                await _recompute_positions()

            # Check again in 5 minutes
            await asyncio.sleep(300)
        except Exception:
            logger.exception("Error refreshing betas")
            await asyncio.sleep(60)


async def _save_gex_cache() -> None:
    """Persist GEX profiles to the database."""
    if not _gex_profiles:
        return
    import dataclasses

    cache = {}
    for ticker, profile in _gex_profiles.items():
        cache[ticker] = {
            "underlying": profile.underlying,
            "spot_price": profile.spot_price,
            "net_gex": profile.net_gex,
            "call_wall": profile.call_wall,
            "put_wall": profile.put_wall,
            "flip_point": profile.flip_point,
            "expirations": profile.expirations,
            "fetched_at": profile.fetched_at,
            "strikes": [dataclasses.asdict(gs) for gs in profile.strikes],
        }
    await db.set_setting(settings.data_dir, "gex_cache", json.dumps(cache))
    logger.info("GEX cache saved (%d profiles)", len(cache))


async def _load_gex_cache() -> None:
    """Load cached GEX profiles from the database."""
    from positionoracle.types import GEXStrike

    raw = await db.get_setting(settings.data_dir, "gex_cache")
    if not raw:
        return

    cache = json.loads(raw)
    for ticker, data in cache.items():
        _gex_profiles[ticker] = GEXProfile(
            underlying=data["underlying"],
            spot_price=data["spot_price"],
            net_gex=data["net_gex"],
            call_wall=data["call_wall"],
            put_wall=data["put_wall"],
            flip_point=data["flip_point"],
            expirations=data.get("expirations", []),
            fetched_at=data.get("fetched_at", ""),
            strikes=[
                GEXStrike(**gs) for gs in data.get("strikes", [])
            ],
        )
    logger.info(
        "Loaded cached GEX data (%d profiles, from %s)",
        len(_gex_profiles),
        next(iter(_gex_profiles.values())).fetched_at[:10] if _gex_profiles else "?",
    )


async def _refresh_gex() -> None:
    """Fetch options chain snapshots and compute GEX profiles for all underlyings."""
    global http_client

    if not _positions or not settings.massive_api_key:
        return

    if http_client is None:
        http_client = httpx.AsyncClient(timeout=30)

    # Always include SPY; process it first so it always appears
    others = sorted({p.underlying for p in _positions} - {"SPY"})
    underlyings = ["SPY", *others]

    # Ensure we have spot prices before computing GEX
    for underlying in underlyings:
        if underlying not in _underlying_prices or not _underlying_prices[underlying]:
            stock_snap = await massive.get_stock_snapshot(
                settings.massive_api_key, underlying, client=http_client,
            )
            if stock_snap:
                day_agg = stock_snap.get("day", {})
                prev_day = stock_snap.get("prevDay", {})
                last_trade = stock_snap.get("lastTrade", {})
                price = (
                    last_trade.get("p", 0)
                    or day_agg.get("c", 0)
                    or prev_day.get("c", 0)
                )
                if price:
                    _underlying_prices[underlying] = price
                    logger.info("GEX: got spot for %s: %.2f", underlying, price)

    today = datetime.date.today()
    default_expiry = (today + datetime.timedelta(days=7)).isoformat()

    for underlying in underlyings:
        try:
            spot = _underlying_prices.get(underlying, 0.0)
            if not spot:
                logger.warning("GEX: no spot price for %s, skipping", underlying)
                continue

            # Compute strike range and expiration horizon from held options
            option_positions = [
                p for p in _positions
                if p.underlying == underlying
                and p.contract_type != ContractType.STOCK
            ]
            option_strikes = [p.strike for p in option_positions]

            if option_positions:
                # Match the furthest expiration of held options
                max_expiry = max(
                    p.expiration for p in option_positions
                ).isoformat()
            else:
                max_expiry = default_expiry

            strike_gte, strike_lte = gex.compute_strike_range(
                spot, option_strikes or None,
            )
            logger.info(
                "GEX: fetching chain for %s, spot=%.2f, range=%.0f-%.0f, exp<=%s",
                underlying, spot, strike_gte, strike_lte, max_expiry,
            )

            chain_data = await massive.get_options_chain_snapshot(
                settings.massive_api_key,
                underlying,
                strike_gte=strike_gte,
                strike_lte=strike_lte,
                expiration_lte=max_expiry,
                client=http_client,
            )

            if chain_data:
                chain_data = gex.filter_chain_data(
                    chain_data, strike_gte, strike_lte,
                )
                _gex_profiles[underlying] = gex.build_gex_profile(
                    underlying, spot, chain_data,
                )
            else:
                logger.warning("GEX: no chain data for %s", underlying)
        except Exception:
            logger.exception("GEX: error fetching chain for %s", underlying)

    # Persist to DB
    await _save_gex_cache()

    # Broadcast updated data
    await _recompute_positions()


async def _gex_loop() -> None:
    """Refresh GEX data once per trading day.

    Fetches as soon as it detects no data for today on a weekday,
    whether that's at 9:45 ET or any time after.
    """
    from zoneinfo import ZoneInfo

    while True:
        try:
            et_now = datetime.datetime.now(tz=ZoneInfo("America/New_York"))

            if et_now.weekday() < 5:
                today = et_now.date().isoformat()
                last_fetch = ""
                if _gex_profiles:
                    any_profile = next(iter(_gex_profiles.values()), None)
                    if any_profile and any_profile.fetched_at:
                        last_fetch = any_profile.fetched_at[:10]

                if last_fetch != today:
                    logger.info("GEX: daily refresh triggered")
                    await _refresh_gex()

            # Check again in 5 minutes
            await asyncio.sleep(300)
        except Exception:
            logger.exception("Error in GEX loop")
            await asyncio.sleep(60)


async def _refresh_options_snapshots() -> None:
    """Fetch fresh Greeks/IV from Massive for each option position."""
    global http_client

    if not _positions or not settings.massive_api_key:
        return

    if http_client is None:
        http_client = httpx.AsyncClient(timeout=30)

    # Refresh daily closes used for VRP realized-vol. Cheap when
    # already cached for today; one Massive call per ticker otherwise.
    await _refresh_underlying_closes()

    r = 0.05  # Approximate risk-free rate

    # Fetch underlying prices — always during market hours, only if missing otherwise.
    # Always include SPY for beta-weighted delta calculations.
    if _is_market_open():
        underlyings_needed = {p.underlying for p in _positions} | {"SPY"}
    else:
        underlyings_needed = {
            p.underlying for p in _positions
            if p.underlying not in _underlying_prices or _underlying_prices[p.underlying] == 0
        }
        if "SPY" not in _underlying_prices or _underlying_prices["SPY"] == 0:
            underlyings_needed.add("SPY")
    for underlying in underlyings_needed:
        stock_snap = await massive.get_stock_snapshot(
            settings.massive_api_key, underlying, client=http_client,
        )
        if stock_snap:
            # During market hours use the freshest price available;
            # after hours use the regular-session day close.
            last_trade = stock_snap.get("lastTrade", {})
            minute_agg = stock_snap.get("min", {})
            day_agg = stock_snap.get("day", {})
            prev_day = stock_snap.get("prevDay", {})
            if _is_market_open():
                price = (
                    last_trade.get("p", 0)
                    or minute_agg.get("c", 0)
                    or day_agg.get("c", 0)
                    or stock_snap.get("todaysChange", 0) + prev_day.get("c", 0)
                    or prev_day.get("c", 0)
                )
            else:
                price = (
                    day_agg.get("c", 0)
                    or prev_day.get("c", 0)
                )
            if price:
                _underlying_prices[underlying] = price
                logger.info(
                    "Got stock price for %s: %.2f "
                    "(lastTrade=%.2f min=%.2f day=%.2f prevDay=%.2f)",
                    underlying, price,
                    last_trade.get("p", 0), minute_agg.get("c", 0),
                    day_agg.get("c", 0), prev_day.get("c", 0),
                )
            else:
                logger.warning(
                    "No usable price for %s: lastTrade=%s prevDay=%s",
                    underlying, last_trade, prev_day,
                )
        else:
            logger.warning("Stock snapshot returned no data for %s", underlying)

    for pos in _positions:
        if pos.contract_type == ContractType.STOCK:
            continue

        massive_ticker = flex.build_massive_ticker(pos)
        logger.info("Fetching snapshot for %s", massive_ticker)

        snap = await massive.get_option_contract_snapshot(
            settings.massive_api_key,
            pos.underlying,
            massive_ticker,
            client=http_client,
        )

        if snap:
            greeks_data = snap.get("greeks", {})
            iv = snap.get("implied_volatility", 0.0)
            underlying_data = snap.get("underlying_asset", {})
            u_price = underlying_data.get(
                "price", _underlying_prices.get(pos.underlying, 0.0),
            )
            dte = (pos.expiration - datetime.date.today()).days
            t = max(dte / 365, 0.001)

            greeks = compute_greeks_from_massive(
                s=u_price,
                k=pos.strike,
                t=t,
                r=r,
                contract_type=pos.contract_type,
                delta=greeks_data.get("delta", 0.0),
                gamma=greeks_data.get("gamma", 0.0),
                theta=greeks_data.get("theta", 0.0),
                vega=greeks_data.get("vega", 0.0),
                iv=iv,
            )

            last_quote = snap.get("last_quote", {})
            bid = last_quote.get("bid", 0)
            ask = last_quote.get("ask", 0)
            mid = (bid + ask) / 2 if bid and ask else None

            _position_greeks[pos.symbol] = PositionGreeks(
                position=pos,
                greeks=greeks,
                underlying_price=u_price,
                option_mid=mid,
            )

            if u_price:
                _underlying_prices[pos.underlying] = u_price
            logger.info(
                "Got Greeks for %s: delta=%.4f iv=%.4f vanna=%.6f charm=%.6f vomma=%.6f",
                massive_ticker, greeks.delta, iv,
                greeks.vanna, greeks.charm, greeks.vomma,
            )
        else:
            logger.warning("No snapshot for %s", massive_ticker)
            if pos.symbol not in _position_greeks:
                _position_greeks[pos.symbol] = PositionGreeks(
                    position=pos,
                    greeks=Greeks(),
                    underlying_price=_underlying_prices.get(pos.underlying, 0.0),
                )

    await _recompute_positions()


def _serialize_summaries(summaries: dict[str, Any]) -> dict[str, Any]:
    """Serialize portfolio summaries for JSON broadcast.

    Parameters
    ----------
    summaries : dict[str, Any]
        Portfolio summaries keyed by underlying.

    Returns
    -------
    dict[str, Any]
        JSON-serializable dictionary.
    """
    result: dict[str, Any] = {
        "type": "update",
        "last_updated": datetime.datetime.now(tz=datetime.UTC).isoformat(),
        "last_report_generated": _last_report_generated,
        "market_open": _is_market_open(),
        "underlyings": {},
    }

    for ticker, summary in summaries.items():
        positions_data = []
        for pg in summary.positions:
            pos = pg.position
            positions_data.append({
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
            })

        advice_data = [dataclasses.asdict(a) for a in summary.advice]
        for a in advice_data:
            a["level"] = a["level"].value if hasattr(a["level"], "value") else a["level"]

        betas = _beta_data.get("betas", {})
        spy_price: float = _underlying_prices.get("SPY") or _beta_data.get("spy_price") or 0.0
        ticker_beta = betas.get(ticker, 1.0)
        u_price = _underlying_prices.get(ticker, 0.0)
        bw_delta = beta.beta_weighted_delta(
            summary.net_delta, u_price, ticker_beta, spy_price,
        )

        result["underlyings"][ticker] = {
            "net_delta": summary.net_delta,
            "net_gamma": summary.net_gamma,
            "net_theta": summary.net_theta,
            "net_vega": summary.net_vega,
            "beta": ticker_beta,
            "beta_weighted_delta": bw_delta,
            "positions": positions_data,
            "advice": advice_data,
        }

    # Portfolio-level rollup
    portfolio_bw_delta = sum(
        u.get("beta_weighted_delta", 0.0) for u in result["underlyings"].values()
    )
    result["portfolio"] = {
        "net_delta": sum(s.net_delta for s in summaries.values()),
        "net_gamma": sum(s.net_gamma for s in summaries.values()),
        "net_theta": sum(s.net_theta for s in summaries.values()),
        "net_vega": sum(s.net_vega for s in summaries.values()),
        "beta_weighted_delta": portfolio_bw_delta,
        "spy_price": _underlying_prices.get("SPY") or _beta_data.get("spy_price") or 0.0,
    }

    # GEX profiles
    if _gex_profiles:
        result["gex"] = {}
        for ticker, profile in _gex_profiles.items():
            result["gex"][ticker] = {
                "underlying": profile.underlying,
                "spot_price": profile.spot_price,
                "net_gex": profile.net_gex,
                "call_wall": profile.call_wall,
                "put_wall": profile.put_wall,
                "flip_point": profile.flip_point,
                "expirations": profile.expirations,
                "fetched_at": profile.fetched_at,
                "strikes": [
                    {
                        "strike": gs.strike,
                        "call_gex": gs.call_gex,
                        "put_gex": gs.put_gex,
                        "net_gex": gs.net_gex,
                        "call_oi": gs.call_oi,
                        "put_oi": gs.put_oi,
                    }
                    for gs in profile.strikes
                ],
            }

    return result


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application startup and shutdown lifecycle."""
    global settings, _positions, stock_ws, http_client, _snapshot_task, _beta_data, _gex_task

    settings = get_settings()
    await db.init_db(settings.data_dir)
    await _reload_positions()
    await _reload_blacklist()
    await _reload_position_entries()
    logger.info(
        "Loaded %d positions, %d blacklist entries, %d entry-data records",
        len(_positions),
        len(_blacklist),
        len(_position_entries),
    )

    # Load cached betas
    cached = await beta.load_cached_betas(settings.data_dir)
    if cached:
        _beta_data = cached
        logger.info("Loaded cached betas from %s", cached.get("computed_at", "?"))

    await _load_gex_cache()

    yield

    # Shutdown
    if stock_ws:
        await stock_ws.disconnect()
    if _snapshot_task:
        _snapshot_task.cancel()
    if _beta_task:
        _beta_task.cancel()
    if _gex_task:
        _gex_task.cancel()
    if http_client:
        await http_client.aclose()


app = FastAPI(
    title="PositionOracle",
    version="1.0",
    description=(
        "PositionOracle internal and public REST API.\n\n"
        "Browser-facing routes (`/api/auth/...`, `/api/positions/...`, etc.) "
        "are authenticated via a signed session cookie set by the passkey "
        "login flow.\n\n"
        "Public REST routes under `/api/v1/` are authenticated via a Bearer "
        "API key — generate one at `POST /api/keys` after logging in, then "
        "send it as `Authorization: Bearer po_...`."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Bearer auth scheme used by /api/v1/* endpoints. auto_error=False lets
# us surface a uniform 401 from the dependency rather than FastAPI's
# default 403.
_bearer = HTTPBearer(auto_error=False, scheme_name="API Key")


async def _require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ApiKey:
    """Validate the Bearer API key and return the matched record.

    Updates ``last_used_at`` on success. Raises 401 on any failure
    (missing header, unknown key, malformed token).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials.strip()
    digest = api_keys_mod.hash_key(token)
    record = await db.lookup_api_key_by_hash(settings.data_dir, digest)
    if record is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await db.touch_api_key(settings.data_dir, record.id)
    return record


# Serve SvelteKit static build
_static_dir = Path(__file__).parent.parent / "frontend" / "build"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/api/auth/status")
async def auth_status(request: Request) -> JSONResponse:
    """Check authentication status."""
    cookie = request.cookies.get(_COOKIE_NAME)
    authenticated = _verify_session(cookie)
    creds = auth.load_credentials(settings.data_dir)
    return JSONResponse({
        "authenticated": authenticated,
        "has_credentials": len(creds) > 0,
    })


@app.post("/api/auth/register/begin")
async def register_begin(
    request: Request,
    setup_token: str | None = Query(default=None),
) -> JSONResponse:
    """Begin passkey registration ceremony."""
    creds = auth.load_credentials(settings.data_dir)

    # Allow registration if: valid setup token OR active session
    has_valid_token = setup_token == settings.setup_token
    cookie = request.cookies.get(_COOKIE_NAME)
    has_session = _verify_session(cookie)

    if not has_valid_token and not has_session:
        raise HTTPException(status_code=403, detail="Invalid setup token or not authenticated")

    options_json, challenge_token = auth.begin_registration(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        creds=creds,
    )
    return JSONResponse({
        "options": json.loads(options_json),
        "challenge_token": challenge_token,
    })


@app.post("/api/auth/register/complete")
async def register_complete(request: Request) -> JSONResponse:
    """Complete passkey registration ceremony."""
    body = await request.json()
    credential = body.get("credential")
    challenge_token = body.get("challenge_token", "")
    name = body.get("name", "Default Key")

    try:
        record = auth.complete_registration(
            credential_json=credential,
            challenge_token=challenge_token,
            rp_id=settings.rp_id,
            expected_origin=settings.expected_origin,
            name=name,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    creds = auth.load_credentials(settings.data_dir)
    creds.append(record)
    auth.save_credentials(settings.data_dir, creds)

    response = JSONResponse({"status": "ok", "credential_name": record["name"]})
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_create_session_cookie(),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
    )
    return response


@app.post("/api/auth/login/begin")
async def login_begin() -> JSONResponse:
    """Begin passkey authentication ceremony."""
    creds = auth.load_credentials(settings.data_dir)
    if not creds:
        raise HTTPException(status_code=404, detail="No credentials registered")

    options_json, challenge_token = auth.begin_authentication(
        rp_id=settings.rp_id,
        creds=creds,
    )
    return JSONResponse({
        "options": json.loads(options_json),
        "challenge_token": challenge_token,
    })


@app.post("/api/auth/login/complete")
async def login_complete(request: Request) -> JSONResponse:
    """Complete passkey authentication ceremony."""
    body = await request.json()
    credential = body.get("credential")
    challenge_token = body.get("challenge_token", "")

    creds = auth.load_credentials(settings.data_dir)
    matched = auth.complete_authentication(
        credential_json=credential,
        challenge_token=challenge_token,
        rp_id=settings.rp_id,
        expected_origin=settings.expected_origin,
        creds=creds,
    )
    if matched is None:
        raise HTTPException(status_code=401, detail="Authentication failed")

    auth.save_credentials(settings.data_dir, creds)

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_create_session_cookie(),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
    )
    return response


@app.post("/api/auth/logout")
async def logout() -> JSONResponse:
    """Clear the session cookie and stop market data."""
    await _stop_market_data()
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# API key management (session-authenticated)
# ---------------------------------------------------------------------------


def _api_key_to_list_item(k: ApiKey) -> ApiKeyListItem:
    """Convert a stored ApiKey to its public list-item form."""
    return ApiKeyListItem(
        id=k.id,
        name=k.name,
        key_prefix=k.key_prefix,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
    )


@app.post(
    "/api/keys",
    response_model=ApiKeyCreated,
    summary="Generate a new API key",
    tags=["keys"],
)
async def create_api_key(
    request: Request,
    body: CreateApiKeyRequest,
) -> ApiKeyCreated:
    """Generate a new API key for the authenticated user.

    The cleartext ``key`` is returned **once** in the response — store
    it somewhere safe. Subsequent listings show only the 8-character
    ``key_prefix``.
    """
    _require_auth(request)
    cleartext, digest, prefix = api_keys_mod.generate_key()
    record = await db.insert_api_key(
        settings.data_dir,
        name=body.name.strip(),
        key_prefix=prefix,
        key_hash=digest,
    )
    logger.info("API key created: id=%d name=%r prefix=%s", record.id, record.name, prefix)
    return ApiKeyCreated(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        key=cleartext,
        created_at=record.created_at,
    )


@app.get(
    "/api/keys",
    response_model=ApiKeyList,
    summary="List API keys",
    tags=["keys"],
)
async def list_api_keys_endpoint(request: Request) -> ApiKeyList:
    """List all API keys for the authenticated user (cleartext omitted)."""
    _require_auth(request)
    keys = await db.list_api_keys(settings.data_dir)
    return ApiKeyList(keys=[_api_key_to_list_item(k) for k in keys])


@app.delete(
    "/api/keys/{key_id}",
    summary="Revoke an API key",
    tags=["keys"],
)
async def delete_api_key_endpoint(request: Request, key_id: int) -> JSONResponse:
    """Revoke (delete) a single API key by id."""
    _require_auth(request)
    deleted = await db.delete_api_key(settings.data_dir, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="API key not found")
    logger.info("API key revoked: id=%d", key_id)
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Public REST API v1 (Bearer-authenticated)
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/positions",
    response_model=PositionsResponse,
    summary="Get positions with computed stats",
    tags=["v1"],
)
async def v1_positions(
    api_key: ApiKey = Depends(_require_api_key),
) -> PositionsResponse:
    """Return all positions with Greeks, VRP, P&L%, and portfolio rollup.

    The payload is byte-identical to what the in-app WebSocket
    broadcasts — same field names, same nesting, same units. Refer to
    the ``PositionsResponse`` schema below for the full structure.
    """
    thresholds = await db.get_thresholds(settings.data_dir)
    all_pgs = list(_position_greeks.values())
    summaries = build_portfolio_summary(all_pgs, thresholds, _gex_profiles)
    payload = _serialize_summaries(summaries)
    return PositionsResponse.model_validate(payload)


@app.get(
    "/api/v1/washsale",
    response_model=WashsaleResponse,
    summary="Get the wash-sale blacklist",
    tags=["v1"],
)
async def v1_washsale(
    api_key: ApiKey = Depends(_require_api_key),
) -> WashsaleResponse:
    """Return symbols inside their IRS 30-day wash-sale window.

    Buying any returned ``symbol`` before its ``expires`` date will
    trigger a wash sale on a previously realized loss.
    """
    await _reload_blacklist()
    today_et = datetime.datetime.now(tz=_ET).date()
    entries = [
        {
            "symbol": e.symbol,
            "loss_date": e.loss_date.isoformat(),
            "expires": e.expires.isoformat(),
            "days_remaining": max(0, (e.expires - today_et).days),
        }
        for e in _blacklist
    ]
    return WashsaleResponse(
        entries=entries,
        last_report_generated=_last_report_generated,
    )


def _synthesize_option_symbol(
    underlying: str,
    expiration: datetime.date,
    contract_type: ContractType,
    strike: float,
) -> str:
    """Build an IB-OCC-style option symbol.

    IB's Flex export emits options as ``"AAPL  251219C00150000"`` —
    underlying space-padded to 6 chars, YYMMDD, ``C``/``P``, strike
    times 1000 zero-padded to 8 digits. Matching that exactly means a
    later Flex sync upserts the row in place when IB books the trade.
    """
    underlying_padded = underlying.upper().ljust(6)
    expiry_str = expiration.strftime("%y%m%d")
    put_call = "C" if contract_type == ContractType.CALL else "P"
    strike_int = round(strike * 1000)
    return f"{underlying_padded}{expiry_str}{put_call}{strike_int:08d}"


def _ensure_entry_time_aware(entry_time: datetime.datetime) -> datetime.datetime:
    """Treat naive ``entry_time`` values as ET, per the API docs."""
    if entry_time.tzinfo is None:
        return entry_time.replace(tzinfo=_ET)
    return entry_time


@app.post(
    "/api/v1/positions",
    response_model=CreatedPositionResponse,
    status_code=201,
    summary="Record an intraday position",
    tags=["v1"],
)
async def v1_create_position(
    body: CreatePositionRequest,
    api_key: ApiKey = Depends(_require_api_key),
) -> CreatedPositionResponse:
    """Insert a position recorded outside the IB Flex pipeline.

    Synchronously fetches the entry spot (Massive 1-min bar), entry
    risk-free rate (FRED), and back-solves entry IV via Black-Scholes
    so VRP and P&L% are populated immediately. Also kicks the snapshot
    loop so live Greeks / IV / spot for the new position are fetched
    before the response returns.

    **Reconciliation warning**: the next IB Flex sync upserts by
    symbol. If IB has the trade, the manual entry is updated in
    place. If IB hasn't booked it yet, the sync deletes the manual
    entry as part of its cleanup step. Trigger a sync only after IB
    has caught up.
    """
    global http_client, _positions

    contract_type_str = body.contract_type.lower()
    if contract_type_str == "stock":
        if body.quantity == 0:
            raise HTTPException(status_code=400, detail="Quantity must be non-zero")
        contract_type = ContractType.STOCK
        symbol = body.underlying.upper()
        strike = 0.0
        expiration = datetime.date.max
        multiplier = body.multiplier or 1
    else:
        if body.strike is None or body.expiration is None:
            raise HTTPException(
                status_code=400,
                detail="strike and expiration are required for option positions",
            )
        if body.expiration < datetime.date.today():
            raise HTTPException(
                status_code=400,
                detail="Cannot create a position on an expired contract",
            )
        if body.quantity == 0:
            raise HTTPException(status_code=400, detail="Quantity must be non-zero")
        contract_type = (
            ContractType.CALL if contract_type_str == "call" else ContractType.PUT
        )
        strike = body.strike
        expiration = body.expiration
        multiplier = body.multiplier or 100
        symbol = _synthesize_option_symbol(
            body.underlying, expiration, contract_type, strike,
        )

    # Reject duplicate symbols outright so the caller sees a clean 409
    # rather than a silent overwrite.
    if any(p.symbol == symbol for p in _positions):
        raise HTTPException(
            status_code=409,
            detail=f"Position with symbol {symbol!r} already exists",
        )

    entry_time = _ensure_entry_time_aware(body.entry_time)
    qty_sign = 1 if body.quantity > 0 else -1
    cost_basis = -1 * qty_sign * body.entry_premium_per_share * abs(body.quantity) * multiplier

    position = Position(
        symbol=symbol,
        underlying=body.underlying.upper(),
        contract_type=contract_type,
        strike=strike,
        expiration=expiration,
        quantity=body.quantity,
        cost_basis=cost_basis,
        multiplier=multiplier,
    )

    await db.upsert_positions(settings.data_dir, [*_positions, position])
    await _reload_positions()

    entry_record: PositionEntry | None = None
    if contract_type != ContractType.STOCK:
        if http_client is None:
            http_client = httpx.AsyncClient(timeout=30)
        synthetic_open = OpeningTrade(
            symbol=symbol,
            underlying=position.underlying,
            trade_datetime=entry_time,
            trade_price=body.entry_premium_per_share,
            quantity=body.quantity,
        )
        try:
            entry_record = await _compute_position_entry(
                position, synthetic_open, client=http_client,
            )
        except Exception as exc:
            logger.exception("Manual entry-data backfill failed for %s", symbol)
            await db.delete_position(settings.data_dir, symbol)
            await _reload_positions()
            raise HTTPException(
                status_code=502,
                detail=f"Failed to resolve entry data: {exc}",
            ) from exc

        if entry_record is None:
            await db.delete_position(settings.data_dir, symbol)
            await _reload_positions()
            raise HTTPException(
                status_code=502,
                detail=(
                    "Failed to resolve entry spot from Massive 1-min bars. "
                    "Verify the entry_time falls inside US market hours and "
                    "that Massive has data for the underlying."
                ),
            )

        await db.upsert_position_entry(settings.data_dir, entry_record)
        _position_entries[symbol] = entry_record

    await _init_position_greeks()
    await _ensure_market_data()
    # Synchronously refresh snapshots so Greeks/IV/spot populate before
    # we respond. Idempotent and fast for a single fresh ticker.
    if settings.massive_api_key:
        try:
            await _refresh_options_snapshots()
        except Exception:
            logger.exception(
                "Snapshot refresh after manual insert failed (position still "
                "inserted, VRP/P&L will populate on next snapshot tick)",
            )
    else:
        await _recompute_positions()

    logger.info("Manual position created via API: %s qty=%d", symbol, body.quantity)
    return CreatedPositionResponse(
        symbol=symbol,
        underlying=position.underlying,
        contract_type=contract_type.value,
        strike=strike,
        expiration=expiration.isoformat(),
        quantity=body.quantity,
        multiplier=multiplier,
        entry_time=entry_record.entry_time if entry_record else None,
        entry_spot=entry_record.entry_spot if entry_record else None,
        entry_premium_per_share=(
            entry_record.entry_premium_per_share if entry_record else None
        ),
        entry_iv=entry_record.entry_iv if entry_record else None,
        entry_rate=entry_record.entry_rate if entry_record else None,
    )


@app.delete(
    "/api/v1/positions/{symbol}",
    status_code=204,
    summary="Close (delete) a position",
    tags=["v1"],
)
async def v1_delete_position(
    symbol: str,
    api_key: ApiKey = Depends(_require_api_key),
) -> None:
    """Remove a position by its exact symbol.

    Also deletes the cached ``position_entry`` row and evicts the
    in-memory caches, then broadcasts the updated portfolio over the
    WebSocket. Returns 404 if the symbol isn't present.
    """
    deleted = await db.delete_position(settings.data_dir, symbol)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Position with symbol {symbol!r} not found",
        )

    # Prune the position_entry row too — orphan rows are harmless but
    # we keep things tidy.
    await db.delete_position_entries_not_in(
        settings.data_dir,
        {p.symbol for p in _positions if p.symbol != symbol},
    )
    _position_entries.pop(symbol, None)
    _position_greeks.pop(symbol, None)

    await _reload_positions()
    await _recompute_positions()
    logger.info("Position closed via API: %s", symbol)


# ---------------------------------------------------------------------------
# Position routes
# ---------------------------------------------------------------------------


@app.post("/api/positions/import")
async def import_positions(request: Request, file: UploadFile) -> JSONResponse:
    """Import positions from an IB Flex Query XML file upload.

    Parameters
    ----------
    request : Request
        The incoming request (must be authenticated).
    file : UploadFile
        The Flex Query XML file.

    Returns
    -------
    JSONResponse
        Import result with count of positions.
    """
    global _positions

    _require_auth(request)

    content = await file.read()
    xml_str = content.decode("utf-8")
    report = flex.parse_flex_report(xml_str)

    if not report.positions:
        raise HTTPException(status_code=400, detail="No option positions found in file")

    count = await db.upsert_positions(settings.data_dir, report.positions)
    await db.set_setting(
        settings.data_dir,
        _FLEX_REPORT_DATE_KEY,
        report.when_generated.isoformat(),
    )
    await db.bulk_upsert_blacklist(settings.data_dir, report.losses)
    await _reload_positions()
    await _reload_blacklist()
    await _reload_position_entries()
    await _backfill_entry_data(report.opening_trades)

    await _init_position_greeks()
    await _recompute_positions()
    await _ensure_market_data()

    return JSONResponse({"status": "ok", "imported": count})


def _previous_business_day(d: datetime.date) -> datetime.date:
    """Return the most recent weekday strictly before ``d``."""
    prev = d - datetime.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= datetime.timedelta(days=1)
    return prev


def _expected_latest_report_date(now_et: datetime.datetime) -> datetime.date:
    """Most recent ET date for which IB should have a published Flex report.

    IB publishes EOD reports after market close. Before ``_FLEX_PUBLISH_HOUR_ET``
    on a weekday, today's report isn't ready yet, so the latest expected
    report is the previous business day. On weekends, the latest is Friday.

    Parameters
    ----------
    now_et : datetime.datetime
        Current time in market timezone.

    Returns
    -------
    datetime.date
        The expected date of the most recent published report.
    """
    today = now_et.date()
    if today.weekday() >= 5 or now_et.hour < _FLEX_PUBLISH_HOUR_ET:
        return _previous_business_day(today)
    return today


def _entry_premium_per_share(pos: Position) -> float:
    """Per-share entry premium derived from cost basis.

    Cost basis is weighted across all opening lots, so this stays
    accurate for averaged-in positions. Always positive.
    """
    if pos.quantity == 0 or pos.multiplier == 0:
        return 0.0
    return abs(pos.cost_basis) / (abs(pos.quantity) * pos.multiplier)


async def _compute_position_entry(
    pos: Position,
    open_trade: OpeningTrade | None,
    *,
    client: httpx.AsyncClient,
) -> PositionEntry | None:
    """Resolve entry spot/IV/rate for a single option position.

    Returns ``None`` if any required data is missing (no opening trade,
    no minute bar, no FRED key, etc.).
    """
    if pos.contract_type == ContractType.STOCK:
        return None
    if open_trade is None:
        logger.warning(
            "VRP backfill: no opening trade for %s — skipping", pos.symbol,
        )
        return None

    entry_time = open_trade.trade_datetime
    trade_day = entry_time.date().isoformat()

    bars = await massive.get_minute_bars(
        settings.massive_api_key, pos.underlying, trade_day, client=client,
    )
    if not bars:
        logger.warning(
            "VRP backfill: no 1-min bars for %s on %s — skipping",
            pos.underlying, trade_day,
        )
        return None

    target_ms = int(entry_time.astimezone(datetime.UTC).timestamp() * 1000)
    bar = massive.pick_bar_for_minute(bars, target_ms)
    if bar is None:
        logger.warning(
            "VRP backfill: no bar near %s for %s — skipping",
            entry_time.isoformat(), pos.symbol,
        )
        return None

    high = bar.get("h")
    low = bar.get("l")
    if high is None or low is None:
        return None
    entry_spot = (float(high) + float(low)) / 2.0

    premium = _entry_premium_per_share(pos)
    if premium <= 0:
        logger.warning(
            "VRP backfill: non-positive entry premium for %s — skipping", pos.symbol,
        )
        return None

    dte_at_entry = max((pos.expiration - entry_time.date()).days, 1)
    rate = await fred.get_rate_for_dte(
        settings.fred_api_key, settings.data_dir, dte_at_entry, client=client,
    )

    t_years = dte_at_entry / 365.0
    iv = vrp.implied_vol(
        market_price=premium,
        s=entry_spot,
        k=pos.strike,
        t=t_years,
        r=rate,
        contract_type=pos.contract_type,
    )
    entry_iv: float | None = None if iv is None or iv != iv else iv  # NaN check

    return PositionEntry(
        symbol=pos.symbol,
        underlying=pos.underlying,
        entry_time=entry_time,
        entry_spot=entry_spot,
        entry_premium_per_share=premium,
        entry_iv=entry_iv,
        entry_rate=rate,
        computed_at=datetime.datetime.now(tz=datetime.UTC),
    )


async def _backfill_entry_data(opening_trades: dict[str, OpeningTrade]) -> None:
    """Compute entry data for any open option positions that lack a cache row.

    Idempotent: positions already present in ``_position_entries`` are
    skipped. Orphan rows (positions no longer open) are pruned.
    """
    global http_client, _position_entries

    if not settings.massive_api_key:
        logger.info("VRP backfill skipped — Massive API key not configured")
        return

    if http_client is None:
        http_client = httpx.AsyncClient(timeout=30)

    need_backfill = [
        p for p in _positions
        if p.contract_type != ContractType.STOCK
        and p.symbol not in _position_entries
    ]
    if not need_backfill:
        logger.info("VRP backfill: nothing to do (%d cached)", len(_position_entries))
    else:
        logger.info("VRP backfill: %d position(s) to process", len(need_backfill))

    for pos in need_backfill:
        try:
            entry = await _compute_position_entry(
                pos, opening_trades.get(pos.symbol), client=http_client,
            )
        except Exception:
            logger.exception("VRP backfill failed for %s", pos.symbol)
            continue
        if entry is None:
            continue
        await db.upsert_position_entry(settings.data_dir, entry)
        _position_entries[pos.symbol] = entry
        logger.info(
            "VRP backfill: %s entry_spot=%.2f premium=%.4f iv=%s rate=%.4f",
            pos.symbol,
            entry.entry_spot,
            entry.entry_premium_per_share,
            f"{entry.entry_iv:.4f}" if entry.entry_iv is not None else "nan",
            entry.entry_rate,
        )

    keep = {p.symbol for p in _positions if p.contract_type != ContractType.STOCK}
    pruned = await db.delete_position_entries_not_in(settings.data_dir, keep)
    if pruned:
        logger.info("VRP backfill: pruned %d orphan entry row(s)", pruned)
        _position_entries = await db.load_position_entries(settings.data_dir)


async def _reload_positions() -> None:
    """Delete expired options and refresh ``_positions`` from the DB.

    Run after every successful IB fetch, after any DB mutation that
    might leave stale rows, and at startup. Expiration uses ET so the
    cutoff aligns with market time, not server wall time. Also refreshes
    ``_last_report_generated`` so the WebSocket payload reports the
    timestamp IB stamped on the most recently imported Flex report.
    """
    global _positions, _last_report_generated
    today_et = datetime.datetime.now(tz=_ET).date()
    deleted = await db.delete_expired_positions(settings.data_dir, today_et)
    if deleted:
        logger.info("Removed %d expired option position(s)", deleted)
    _positions = await db.load_positions(settings.data_dir)
    _last_report_generated = await db.get_setting(
        settings.data_dir, _FLEX_REPORT_DATE_KEY,
    )


async def _reload_position_entries() -> None:
    """Refresh ``_position_entries`` from the DB."""
    global _position_entries
    _position_entries = await db.load_position_entries(settings.data_dir)


async def _reload_blacklist() -> None:
    """Prune expired wash-sale entries and refresh ``_blacklist``.

    Called at startup and after any successful Flex Query ingest. Uses
    ET ``today`` so pruning aligns with market timezone, not server wall
    time.
    """
    global _blacklist
    today_et = datetime.datetime.now(tz=_ET).date()
    pruned = await db.prune_blacklist(settings.data_dir, today_et)
    if pruned:
        logger.info("Pruned %d expired wash-sale entry(ies)", pruned)
    _blacklist = await db.load_blacklist(settings.data_dir)


def _flex_response_payload(
    *,
    imported: int,
    cached: bool,
    stale: bool,
    report_generated_at: str | None,
    last_attempt_at: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a uniform response body for ``/api/positions/fetch``."""
    return {
        "status": "ok",
        "imported": imported,
        "cached": cached,
        "stale": stale,
        "report_generated_at": report_generated_at,
        "last_attempt_at": last_attempt_at,
        "error": error,
    }


@app.post("/api/positions/fetch")
async def fetch_positions(
    request: Request,
    force: bool = Query(default=False),
) -> JSONResponse:
    """Fetch positions directly from IB via Flex Query API.

    Cache behavior:

    - If the cached report's date is already at or beyond the latest
      report date IB is expected to have published, no IB call is made.
    - If a previous fetch attempt failed recently (within the backoff
      window), the cached positions are returned with ``stale=true``.
    - On an IB failure with cached data available, return the cached
      list with ``stale=true`` and ``error`` populated (status 200)
      rather than 502 — the UI should still render the book.
    - ``force=true`` bypasses both the date check and the backoff.

    Parameters
    ----------
    request : Request
        The incoming request (must be authenticated).
    force : bool
        If True, bypass the cache and fetch fresh data from IB.

    Returns
    -------
    JSONResponse
        Status, imported count, cache/staleness metadata, and optional
        error message.
    """
    global _positions

    _require_auth(request)

    now_et = datetime.datetime.now(tz=_ET)
    expected_report_date = _expected_latest_report_date(now_et)

    cached_report_raw = await db.get_setting(settings.data_dir, _FLEX_REPORT_DATE_KEY)
    last_attempt_raw = await db.get_setting(settings.data_dir, _FLEX_LAST_ATTEMPT_KEY)
    cached_report_dt = (
        datetime.datetime.fromisoformat(cached_report_raw) if cached_report_raw else None
    )
    last_attempt_dt = (
        datetime.datetime.fromisoformat(last_attempt_raw) if last_attempt_raw else None
    )

    have_current_report = (
        cached_report_dt is not None
        and cached_report_dt.astimezone(_ET).date() >= expected_report_date
        and bool(_positions)
    )

    if not force and have_current_report:
        await _reload_positions()
        await _init_position_greeks()
        await _recompute_positions()
        await _ensure_market_data()
        return JSONResponse(_flex_response_payload(
            imported=len(_positions),
            cached=True,
            stale=False,
            report_generated_at=cached_report_dt.isoformat() if cached_report_dt else None,
            last_attempt_at=last_attempt_dt.isoformat() if last_attempt_dt else None,
        ))

    in_backoff = (
        last_attempt_dt is not None
        and (now_et - last_attempt_dt.astimezone(_ET)).total_seconds() < _FLEX_FAILURE_BACKOFF
    )
    if not force and in_backoff and _positions:
        return JSONResponse(_flex_response_payload(
            imported=len(_positions),
            cached=True,
            stale=True,
            report_generated_at=cached_report_dt.isoformat() if cached_report_dt else None,
            last_attempt_at=last_attempt_dt.isoformat() if last_attempt_dt else None,
            error="Recent IB fetch failed; using cached positions until backoff clears.",
        ))

    if not settings.flex_token or not settings.query_id:
        raise HTTPException(
            status_code=400,
            detail="FLEX_TOKEN and QUERY_ID must be configured in .env",
        )

    attempt_iso = now_et.isoformat()
    try:
        report = await flex.fetch_positions(settings.flex_token, settings.query_id)
    except Exception as exc:
        logger.exception("Failed to fetch Flex Query from IB")
        await db.set_setting(settings.data_dir, _FLEX_LAST_ATTEMPT_KEY, attempt_iso)
        if _positions:
            return JSONResponse(_flex_response_payload(
                imported=len(_positions),
                cached=True,
                stale=True,
                report_generated_at=cached_report_dt.isoformat() if cached_report_dt else None,
                last_attempt_at=attempt_iso,
                error=f"IB Flex Query failed: {exc}",
            ))
        raise HTTPException(status_code=502, detail=f"IB Flex Query failed: {exc}") from exc

    if not report.positions:
        await db.set_setting(settings.data_dir, _FLEX_LAST_ATTEMPT_KEY, attempt_iso)
        raise HTTPException(status_code=400, detail="No option positions found in Flex Query")

    count = await db.upsert_positions(settings.data_dir, report.positions)
    await db.set_setting(
        settings.data_dir,
        _FLEX_REPORT_DATE_KEY,
        report.when_generated.isoformat(),
    )
    await db.set_setting(settings.data_dir, _FLEX_LAST_ATTEMPT_KEY, attempt_iso)
    await db.bulk_upsert_blacklist(settings.data_dir, report.losses)
    await _reload_positions()
    await _reload_blacklist()
    await _reload_position_entries()
    await _backfill_entry_data(report.opening_trades)

    await _init_position_greeks()
    await _recompute_positions()
    await _ensure_market_data()

    return JSONResponse(_flex_response_payload(
        imported=count,
        cached=False,
        stale=False,
        report_generated_at=report.when_generated.isoformat(),
        last_attempt_at=attempt_iso,
    ))


@app.get("/api/positions")
async def list_positions(request: Request) -> JSONResponse:
    """List all positions."""
    _require_auth(request)
    return JSONResponse({
        "positions": [
            {
                "symbol": p.symbol,
                "underlying": p.underlying,
                "contract_type": p.contract_type.value,
                "strike": p.strike,
                "expiration": p.expiration.isoformat(),
                "quantity": p.quantity,
                "cost_basis": p.cost_basis,
                "multiplier": p.multiplier,
            }
            for p in _positions
        ]
    })


@app.delete("/api/positions/{symbol}")
async def delete_position(request: Request, symbol: str) -> JSONResponse:
    """Delete a position by symbol."""
    global _positions

    _require_auth(request)
    deleted = await db.delete_position(settings.data_dir, symbol)
    if not deleted:
        raise HTTPException(status_code=404, detail="Position not found")

    _positions = await db.load_positions(settings.data_dir)
    _position_greeks.pop(symbol, None)
    return JSONResponse({"status": "ok"})


@app.delete("/api/positions")
async def clear_all_positions(request: Request) -> JSONResponse:
    """Delete all positions."""
    global _positions

    _require_auth(request)
    count = await db.clear_positions(settings.data_dir)
    _positions = []
    _position_greeks.clear()
    return JSONResponse({"status": "ok", "deleted": count})


# ---------------------------------------------------------------------------
# Wash-sale routes
# ---------------------------------------------------------------------------


@app.get("/api/washsale/blacklist")
async def get_washsale_blacklist(request: Request) -> JSONResponse:
    """Return the current wash-sale blacklist.

    Always prunes expired entries before responding, so the result
    reflects "today" without waiting for an ingest. Each entry shows
    the most recent realized-loss date for its symbol and the IRS
    30-day window expiry.
    """
    _require_auth(request)
    await _reload_blacklist()
    today_et = datetime.datetime.now(tz=_ET).date()
    return JSONResponse({
        "entries": [
            {
                "symbol": e.symbol,
                "loss_date": e.loss_date.isoformat(),
                "expires": e.expires.isoformat(),
                "days_remaining": max(0, (e.expires - today_et).days),
            }
            for e in _blacklist
        ],
        "last_report_generated": _last_report_generated,
    })


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------


@app.post("/api/gex/refresh")
async def refresh_gex(request: Request) -> JSONResponse:
    """Manually trigger a GEX data refresh (runs in background).

    Returns immediately. Data arrives via WebSocket when ready.

    Returns
    -------
    JSONResponse
        Acknowledgement that the refresh was started.
    """
    _require_auth(request)

    async def _gex_refresh_task() -> None:
        try:
            logger.info("GEX refresh: starting background task")
            logger.info(
                "GEX refresh: positions=%d, api_key=%s, prices=%s",
                len(_positions),
                bool(settings.massive_api_key),
                list(_underlying_prices.keys()),
            )
            await _refresh_gex()
            logger.info(
                "GEX refresh: completed, profiles=%s",
                list(_gex_profiles.keys()),
            )
        except Exception:
            logger.exception("GEX refresh: background task failed")

    task = asyncio.create_task(_gex_refresh_task())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse({"status": "started"})


@app.post("/api/analyze/{underlying}")
async def analyze_underlying(request: Request, underlying: str) -> JSONResponse:
    """Get Claude's analysis of positions for a specific underlying.

    Parameters
    ----------
    request : Request
        The incoming request (must be authenticated).
    underlying : str
        Ticker symbol to analyze.

    Returns
    -------
    JSONResponse
        Claude's analysis as markdown text.
    """
    from positionoracle import claude_advisor

    _require_auth(request)

    # Read fresh settings so model/key changes take effect without restart
    fresh = get_settings()

    if not fresh.anthropic_api_key:
        raise HTTPException(
            status_code=400,
            detail="ANTHROPIC_API_KEY must be configured",
        )

    # Build the current summary for this underlying
    thresholds = await db.get_thresholds(settings.data_dir)
    all_pgs = [pg for pg in _position_greeks.values() if pg.position.underlying == underlying]

    if not all_pgs:
        raise HTTPException(status_code=404, detail=f"No positions for {underlying}")

    from positionoracle.advisor import build_portfolio_summary

    summaries = build_portfolio_summary(all_pgs, thresholds, _gex_profiles)
    summary = summaries.get(underlying)
    if not summary:
        raise HTTPException(status_code=404, detail=f"No summary for {underlying}")

    # Serialize for Claude
    serialized = _serialize_summaries(summaries)
    summary_data = serialized["underlyings"].get(underlying, {})

    spot_price = _underlying_prices.get(underlying, 0.0)
    betas = _beta_data.get("betas", {})
    ticker_beta = betas.get(underlying, 1.0)
    bw_delta = summary_data.get("beta_weighted_delta", 0.0)

    gex_data = serialized.get("gex", {}).get(underlying)

    try:
        analysis = await claude_advisor.analyze_symbol(
            api_key=fresh.anthropic_api_key,
            model=fresh.claude_model,
            underlying=underlying,
            summary=summary_data,
            spot_price=spot_price,
            beta=ticker_beta,
            beta_weighted_delta=bw_delta,
            gex_data=gex_data,
        )
    except Exception as exc:
        logger.exception("Claude analysis failed for %s", underlying)
        raise HTTPException(status_code=502, detail=f"Analysis failed: {exc}") from exc

    return JSONResponse({"underlying": underlying, "analysis": analysis})


# ---------------------------------------------------------------------------
# WebSocket for browser
# ---------------------------------------------------------------------------


@app.websocket("/api/ws")
async def websocket_endpoint(
    ws: WebSocket,
    po_session: str | None = Cookie(default=None),
) -> None:
    """WebSocket endpoint for live portfolio updates."""
    if not _verify_session(po_session):
        await ws.close(code=4001, reason="Not authenticated")
        return

    await manager.connect(ws)

    # Start market data if this is the first connection
    await _ensure_market_data()

    try:
        # Send initial state
        if _position_greeks:
            thresholds = await db.get_thresholds(settings.data_dir)
            all_pgs = list(_position_greeks.values())
            summaries = build_portfolio_summary(all_pgs, thresholds, _gex_profiles)
            await ws.send_json(_serialize_summaries(summaries))

        # Keep alive and handle client messages
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "refresh":
                await _refresh_options_snapshots()
            elif msg.get("type") == "gex_refresh":
                try:
                    await _refresh_gex()
                except Exception:
                    logger.exception("GEX refresh failed via WebSocket")

    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Market data lifecycle
# ---------------------------------------------------------------------------


async def _ensure_market_data() -> None:
    """Start market data connections if not already running."""
    global stock_ws, _snapshot_task, _beta_task, _gex_task

    if not settings.massive_api_key or not _positions:
        logger.info(
            "Market data skipped (api_key=%s, positions=%d)",
            bool(settings.massive_api_key),
            len(_positions),
        )
        return

    underlyings = {p.underlying for p in _positions}

    if stock_ws is None:
        logger.info("Starting stock WebSocket for %s", underlyings)
        stock_ws = massive.StockWebSocket(
            api_key=settings.massive_api_key,
            on_trade=_on_trade,
        )
        await stock_ws.connect()
        await stock_ws.subscribe(underlyings)

    if _snapshot_task is None or _snapshot_task.done():
        logger.info("Starting options snapshot polling")
        _snapshot_task = asyncio.create_task(_snapshot_loop())

    if _beta_task is None or _beta_task.done():
        logger.info("Starting beta computation task")
        _beta_task = asyncio.create_task(_beta_loop())

    if _gex_task is None or _gex_task.done():
        logger.info("Starting GEX refresh task")
        _gex_task = asyncio.create_task(_gex_loop())


async def _stop_market_data() -> None:
    """Stop market data connections when no clients are connected."""
    global stock_ws, _snapshot_task, _beta_task, _gex_task

    if stock_ws:
        await stock_ws.disconnect()
        stock_ws = None

    if _snapshot_task:
        _snapshot_task.cancel()
        _snapshot_task = None

    if _beta_task:
        _beta_task.cancel()
        _beta_task = None

    if _gex_task:
        _gex_task.cancel()
        _gex_task = None

    logger.info("Market data connections stopped (no active clients)")


# ---------------------------------------------------------------------------
# SvelteKit fallback
# ---------------------------------------------------------------------------


@app.get("/{path:path}")
async def serve_frontend(path: str) -> FileResponse:
    """Serve the SvelteKit static build.

    Falls back to index.html for client-side routing.
    """
    static_dir = Path(__file__).parent.parent / "frontend" / "build"

    # Try the exact path first
    file_path = static_dir / path
    if file_path.is_file():
        return FileResponse(file_path)

    # Fall back to index.html for SPA routing
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)

    raise HTTPException(status_code=404, detail="Not found")
