"""FastAPI application for PositionOracle."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import (
    Cookie,
    FastAPI,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from positionoracle import auth, beta, db, flex, gex, massive
from positionoracle.advisor import build_portfolio_summary
from positionoracle.config import Settings, get_settings
from positionoracle.greeks import compute_greeks_from_massive
from positionoracle.types import ContractType, GEXProfile, Greeks, Position, PositionGreeks
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
_snapshot_task: asyncio.Task[None] | None = None
_beta_task: asyncio.Task[None] | None = None
_beta_data: dict[str, Any] = {}  # {"betas": {...}, "spy_price": ..., "computed_at": ...}
_gex_profiles: dict[str, GEXProfile] = {}  # keyed by underlying
_gex_task: asyncio.Task[None] | None = None

_COOKIE_NAME = "po_session"
_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days


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
        if pg and pos.underlying in _underlying_prices:
            pg.underlying_price = _underlying_prices[pos.underlying]

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

    for underlying in underlyings:
        try:
            spot = _underlying_prices.get(underlying, 0.0)
            if not spot:
                logger.warning("GEX: no spot price for %s, skipping", underlying)
                continue

            # Compute strike range from held option positions
            option_strikes = [
                p.strike for p in _positions
                if p.underlying == underlying
                and p.contract_type != ContractType.STOCK
            ]

            strike_gte, strike_lte = gex.compute_strike_range(
                spot, option_strikes or None,
            )
            logger.info(
                "GEX: fetching chain for %s, spot=%.2f, range=%.0f-%.0f",
                underlying, spot, strike_gte, strike_lte,
            )

            chain_data = await massive.get_options_chain_snapshot(
                settings.massive_api_key,
                underlying,
                strike_gte=strike_gte,
                strike_lte=strike_lte,
                client=http_client,
            )

            if chain_data:
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
    """Refresh GEX data once at market open, then hold until next day."""
    from zoneinfo import ZoneInfo

    while True:
        try:
            et_now = datetime.datetime.now(tz=ZoneInfo("America/New_York"))
            minutes = et_now.hour * 60 + et_now.minute

            # Target: 9:45 ET (15 min after open)
            target_minutes = 9 * 60 + 45

            if et_now.weekday() < 5 and minutes >= target_minutes:
                today = et_now.date().isoformat()
                last_fetch = ""
                if _gex_profiles:
                    # Check if we already fetched today
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
                "greeks": dataclasses.asdict(pg.greeks),
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
    _positions = await db.load_positions(settings.data_dir)
    logger.info("Loaded %d positions from database", len(_positions))

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


app = FastAPI(title="PositionOracle", lifespan=lifespan)

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
    positions = flex.parse_flex_xml(xml_str)

    if not positions:
        raise HTTPException(status_code=400, detail="No option positions found in file")

    count = await db.upsert_positions(settings.data_dir, positions)
    _positions = await db.load_positions(settings.data_dir)

    await _init_position_greeks()
    await _recompute_positions()
    await _ensure_market_data()

    return JSONResponse({"status": "ok", "imported": count})


_FLEX_CACHE_TTL = 15 * 60  # 15 minutes
_last_flex_fetch: float = 0.0


@app.post("/api/positions/fetch")
async def fetch_positions(
    request: Request,
    force: bool = Query(default=False),
) -> JSONResponse:
    """Fetch positions directly from IB via Flex Query API.

    Uses the configured ``FLEX_TOKEN`` and ``QUERY_ID`` to download
    the report from IB's servers. Results are cached for 15 minutes
    unless ``force=true`` is passed.

    Parameters
    ----------
    request : Request
        The incoming request (must be authenticated).
    force : bool
        If True, bypass the cache and fetch fresh data from IB.

    Returns
    -------
    JSONResponse
        Import result with count of positions.
    """
    global _positions, _last_flex_fetch

    _require_auth(request)

    now = time.monotonic()
    if not force and (now - _last_flex_fetch) < _FLEX_CACHE_TTL and _positions:
        await _init_position_greeks()
        await _recompute_positions()
        await _ensure_market_data()
        return JSONResponse({
            "status": "ok",
            "imported": len(_positions),
            "cached": True,
        })

    if not settings.flex_token or not settings.query_id:
        raise HTTPException(
            status_code=400,
            detail="FLEX_TOKEN and QUERY_ID must be configured in .env",
        )

    try:
        positions = await flex.fetch_positions(settings.flex_token, settings.query_id)
    except Exception as exc:
        logger.exception("Failed to fetch Flex Query from IB")
        raise HTTPException(status_code=502, detail=f"IB Flex Query failed: {exc}") from exc

    if not positions:
        raise HTTPException(status_code=400, detail="No option positions found in Flex Query")

    _last_flex_fetch = now

    count = await db.upsert_positions(settings.data_dir, positions)
    _positions = await db.load_positions(settings.data_dir)

    await _init_position_greeks()
    await _recompute_positions()
    await _ensure_market_data()

    return JSONResponse({"status": "ok", "imported": count})


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
# Claude analysis
# ---------------------------------------------------------------------------


@app.post("/api/gex/refresh")
async def refresh_gex(request: Request) -> JSONResponse:
    """Manually trigger a GEX data refresh.

    Returns
    -------
    JSONResponse
        Status and number of profiles fetched.
    """
    _require_auth(request)

    await _refresh_gex()

    return JSONResponse({
        "status": "ok",
        "profiles": list(_gex_profiles.keys()),
    })


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
                await _refresh_gex()

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
