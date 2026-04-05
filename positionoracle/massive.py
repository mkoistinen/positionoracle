"""Massive (formerly Polygon) API client for market data."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
import websockets

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_REST_BASE = "https://api.polygon.io"
_WS_STOCKS = "wss://delayed.polygon.io/stocks"
_WS_OPTIONS = "wss://socket.polygon.io/options"


# ---------------------------------------------------------------------------
# REST — Options snapshot (Greeks + IV)
# ---------------------------------------------------------------------------


async def get_option_contract_snapshot(
    api_key: str,
    underlying: str,
    option_ticker: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch the snapshot for a single option contract from Massive.

    Parameters
    ----------
    api_key : str
        Massive API key.
    underlying : str
        Underlying ticker symbol (e.g. ``"AAPL"``).
    option_ticker : str
        Massive-format option ticker (e.g. ``"O:AAPL251219C00150000"``).
    client : httpx.AsyncClient | None
        Optional shared HTTP client.

    Returns
    -------
    dict[str, Any] | None
        Option contract snapshot with Greeks, IV, and quotes, or None on failure.
    """
    url = f"{_REST_BASE}/v3/snapshot/options/{underlying}/{option_ticker}"
    params = {"apiKey": api_key}

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)

    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results")
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch snapshot for %s", option_ticker)
        return None
    finally:
        if close_client:
            await client.aclose()


async def get_stock_snapshot(
    api_key: str,
    ticker: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch a single stock ticker snapshot.

    Parameters
    ----------
    api_key : str
        Massive API key.
    ticker : str
        Stock ticker (e.g. ``"AAPL"``).
    client : httpx.AsyncClient | None
        Optional shared HTTP client.

    Returns
    -------
    dict[str, Any] | None
        Snapshot data or None on failure.
    """
    url = f"{_REST_BASE}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
    params = {"apiKey": api_key}

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)

    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("ticker")
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch stock snapshot for %s", ticker)
        return None
    finally:
        if close_client:
            await client.aclose()


async def get_daily_bars(
    api_key: str,
    ticker: str,
    days: int = 60,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent daily OHLCV bars for a ticker.

    Parameters
    ----------
    api_key : str
        Massive API key.
    ticker : str
        Stock ticker (e.g. ``"SPY"``).
    days : int
        Number of calendar days to look back (default 60).
    client : httpx.AsyncClient | None
        Optional shared HTTP client.

    Returns
    -------
    list[dict[str, Any]]
        List of daily bar dicts with keys ``c`` (close), ``o``, ``h``, ``l``, ``v``, ``t``.
    """
    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=int(days * 1.5))  # Pad for weekends/holidays

    url = f"{_REST_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params: dict[str, str] = {"apiKey": api_key, "adjusted": "true", "sort": "asc"}

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)

    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        # Return only the last N bars
        return results[-days:] if len(results) > days else results
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch daily bars for %s", ticker)
        return []
    finally:
        if close_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# REST — Options chain snapshot (for GEX)
# ---------------------------------------------------------------------------


async def get_options_chain_snapshot(
    api_key: str,
    underlying: str,
    *,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    expiration_lte: str | None = None,
    limit: int = 250,
    max_contracts: int = 2000,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch the full options chain snapshot for an underlying.

    Paginates automatically through all results. Each contract includes
    open interest, greeks, and contract details.

    Parameters
    ----------
    api_key : str
        Massive API key.
    underlying : str
        Underlying ticker (e.g. ``"SPY"``).
    strike_gte : float | None
        Minimum strike price filter.
    strike_lte : float | None
        Maximum strike price filter.
    expiration_lte : str | None
        Maximum expiration date filter (ISO format, e.g. ``"2026-05-01"``).
    limit : int
        Results per page (max 250).
    client : httpx.AsyncClient | None
        Optional shared HTTP client.

    Returns
    -------
    list[dict[str, Any]]
        List of option contract snapshots.
    """
    url = f"{_REST_BASE}/v3/snapshot/options/{underlying}"
    params: dict[str, str] = {"apiKey": api_key, "limit": str(limit)}
    if strike_gte is not None:
        params["strike_price.gte"] = str(strike_gte)
    if strike_lte is not None:
        params["strike_price.lte"] = str(strike_lte)
    if expiration_lte is not None:
        params["expiration_date.lte"] = expiration_lte

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=60)

    results: list[dict[str, Any]] = []
    try:
        while url:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            logger.info(
                "Options chain %s: fetched %d contracts so far",
                underlying, len(results),
            )
            if len(results) >= max_contracts:
                logger.info(
                    "Options chain %s: hit %d contract cap, stopping",
                    underlying, max_contracts,
                )
                break
            next_url = data.get("next_url")
            if next_url:
                # next_url is a full URL; just append the API key
                url = next_url
                params = {"apiKey": api_key}
            else:
                url = ""
    except httpx.HTTPStatusError:
        logger.exception("Failed to fetch options chain for %s", underlying)
    finally:
        if close_client:
            await client.aclose()

    return results


# ---------------------------------------------------------------------------
# WebSocket — Real-time stock prices
# ---------------------------------------------------------------------------


class StockWebSocket:
    """Manages a WebSocket connection to Massive for real-time stock quotes.

    Parameters
    ----------
    api_key : str
        Massive API key.
    on_trade : Callable[[str, float], None] | None
        Callback invoked with ``(ticker, price)`` on each trade.
    """

    def __init__(
        self,
        api_key: str,
        on_trade: Callable[[str, float], Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._on_trade = on_trade
        self._ws: Any = None
        self._subscriptions: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def connect(self) -> None:
        """Open the WebSocket connection and authenticate."""
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Run the WebSocket loop with reconnection."""
        while self._running:
            try:
                async with websockets.connect(_WS_STOCKS) as ws:
                    self._ws = ws
                    logger.info("Stock WebSocket connected")

                    # Authenticate
                    auth_msg = json.dumps({"action": "auth", "params": self._api_key})
                    await ws.send(auth_msg)

                    # Subscribe to second aggregates (A) and trades (T) as fallback
                    if self._subscriptions:
                        channels = ",".join(f"A.{t}" for t in self._subscriptions)
                        sub_msg = json.dumps({
                            "action": "subscribe",
                            "params": channels,
                        })
                        logger.info("Stock WS subscribing: %s", channels)
                        await ws.send(sub_msg)

                    async for raw in ws:
                        if not self._running:
                            break
                        for msg in json.loads(raw):
                            ev = msg.get("ev")
                            status = msg.get("status")
                            if status:
                                logger.info(
                            "Stock WS status: %s - %s", status, msg.get("message", ""),
                        )
                            # A = second aggregate (close price)
                            if ev == "A" and self._on_trade:
                                ticker = msg.get("sym", "")
                                price = msg.get("c", 0.0)
                                if ticker and price:
                                    result = self._on_trade(ticker, price)
                                    if asyncio.iscoroutine(result):
                                        await result
            except Exception:
                if self._running:
                    logger.exception("Stock WebSocket error, reconnecting in 5s")
                    await asyncio.sleep(5)

    async def subscribe(self, tickers: set[str]) -> None:
        """Subscribe to trade events for the given tickers.

        Parameters
        ----------
        tickers : set[str]
            Ticker symbols to subscribe to.
        """
        new = tickers - self._subscriptions
        self._subscriptions |= tickers
        if new and self._ws:
            msg = json.dumps({
                "action": "subscribe",
                "params": ",".join(f"A.{t}" for t in new),
            })
            await self._ws.send(msg)

    async def unsubscribe(self, tickers: set[str]) -> None:
        """Unsubscribe from trade events.

        Parameters
        ----------
        tickers : set[str]
            Ticker symbols to unsubscribe from.
        """
        self._subscriptions -= tickers
        if tickers and self._ws:
            msg = json.dumps({
                "action": "unsubscribe",
                "params": ",".join(f"A.{t}" for t in tickers),
            })
            await self._ws.send(msg)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
