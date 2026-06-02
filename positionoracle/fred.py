"""FRED treasury-yield client with daily caching.

Provides risk-free rates for Black-Scholes inversion (entry-IV
computation in :mod:`positionoracle.vrp`). We fetch the four short-end
constant-maturity Treasury series and pick whichever maturity is closest
to the option's time to expiration.

Yields are returned as continuously-compounded decimals (FRED reports
them as APRs in percent; we convert).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from typing import TYPE_CHECKING

import httpx

from positionoracle import db

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_CACHE_KEY = "fred_treasury_rates"

# Series ID → tenor in days. Keep in ascending tenor order; pick the
# closest match for a given DTE.
_SERIES_BY_DAYS: dict[int, str] = {
    30: "DGS1MO",
    91: "DGS3MO",
    182: "DGS6MO",
    365: "DGS1",
    730: "DGS2",
    1825: "DGS5",
}

_FALLBACK_RATE = 0.05


def _series_for_days(days_to_expiration: int) -> str:
    """Pick the FRED series whose tenor is nearest the given DTE."""
    days = max(days_to_expiration, 1)
    return min(_SERIES_BY_DAYS.items(), key=lambda kv: abs(kv[0] - days))[1]


async def _fetch_latest_observation(
    api_key: str,
    series: str,
    client: httpx.AsyncClient,
) -> float | None:
    """Fetch the most recent non-null observation for a FRED series.

    Returns the value as a decimal (e.g. ``0.0425`` for a 4.25% rate),
    or ``None`` if the API call fails or no value is available.
    """
    params = {
        "series_id": series,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "10",
    }
    try:
        resp = await client.get(_FRED_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError:
        logger.exception("FRED fetch failed for %s", series)
        return None

    for obs in data.get("observations", []):
        raw = obs.get("value", ".")
        if raw and raw != ".":
            try:
                return float(raw) / 100.0
            except ValueError:
                continue
    logger.warning("FRED %s: no non-null observation in last 10 rows", series)
    return None


async def refresh_rates(
    api_key: str,
    data_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, float]:
    """Refresh all tracked treasury rates and cache to the settings table.

    Parameters
    ----------
    api_key : str
        FRED API key.
    data_dir : Path
        Application data directory.
    client : httpx.AsyncClient | None
        Shared HTTP client (created if omitted).

    Returns
    -------
    dict[str, float]
        Mapping of FRED series ID → continuously-compounded rate.
    """
    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)

    rates: dict[str, float] = {}
    try:
        for series in _SERIES_BY_DAYS.values():
            apr = await _fetch_latest_observation(api_key, series, client)
            if apr is None:
                continue
            # Convert simple APR to continuously-compounded rate.
            rates[series] = math.log1p(apr)
    finally:
        if close_client:
            await client.aclose()

    if rates:
        payload = {
            "rates": rates,
            "fetched_at": datetime.date.today().isoformat(),
        }
        await db.set_setting(data_dir, _CACHE_KEY, json.dumps(payload))
        logger.info("FRED: cached %d rates: %s", len(rates), rates)
    else:
        logger.warning("FRED: no rates fetched — leaving cache unchanged")
    return rates


async def load_cached_rates(data_dir: Path) -> dict[str, float] | None:
    """Return cached treasury rates if a cache entry exists.

    Returns
    -------
    dict[str, float] | None
        ``{series_id: rate}`` or ``None`` if no cache.
    """
    raw = await db.get_setting(data_dir, _CACHE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload.get("rates")


async def cached_rates_age_days(data_dir: Path) -> int | None:
    """Days since the cache was last refreshed, or ``None`` if no cache."""
    raw = await db.get_setting(data_dir, _CACHE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    fetched = payload.get("fetched_at")
    if not fetched:
        return None
    try:
        fetched_date = datetime.date.fromisoformat(fetched)
    except ValueError:
        return None
    return (datetime.date.today() - fetched_date).days


async def get_rate_for_dte(
    api_key: str,
    data_dir: Path,
    days_to_expiration: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> float:
    """Return the treasury rate best matching a given DTE.

    Refreshes the cache if it is missing or older than one calendar day.
    Falls back to a static rate (5%) if the API is unreachable and no
    cache exists.

    Parameters
    ----------
    api_key : str
        FRED API key.
    data_dir : Path
        Application data directory.
    days_to_expiration : int
        Calendar days from observation date to option expiration.
    client : httpx.AsyncClient | None
        Optional shared HTTP client.

    Returns
    -------
    float
        Continuously-compounded risk-free rate as a decimal.
    """
    series = _series_for_days(days_to_expiration)

    age = await cached_rates_age_days(data_dir)
    if api_key and (age is None or age >= 1):
        await refresh_rates(api_key, data_dir, client=client)

    cached = await load_cached_rates(data_dir)
    if cached and series in cached:
        return cached[series]
    if cached:
        # Use any available series as a degraded fallback.
        nearest = min(
            cached.items(),
            key=lambda kv: abs(
                next((d for d, s in _SERIES_BY_DAYS.items() if s == kv[0]), 365)
                - max(days_to_expiration, 1),
            ),
        )
        logger.warning(
            "FRED: requested %s missing, falling back to %s",
            series, nearest[0],
        )
        return nearest[1]
    logger.warning("FRED: no cache and no fetch — using fallback %.2f", _FALLBACK_RATE)
    return _FALLBACK_RATE
