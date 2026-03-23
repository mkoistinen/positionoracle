"""Beta computation and caching against SPY."""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

from positionoracle import db, massive

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

logger = logging.getLogger(__name__)

_CACHE_KEY = "betas"
_BENCHMARK = "SPY"


def compute_beta(stock_closes: list[float], bench_closes: list[float]) -> float:
    """Compute beta from aligned daily close prices.

    Parameters
    ----------
    stock_closes : list[float]
        Daily closing prices for the stock.
    bench_closes : list[float]
        Daily closing prices for the benchmark (SPY).

    Returns
    -------
    float
        Beta coefficient. Returns 1.0 if insufficient data.
    """
    n = min(len(stock_closes), len(bench_closes))
    if n < 10:
        return 1.0

    # Align to same length
    sc = stock_closes[-n:]
    bc = bench_closes[-n:]

    # Compute daily returns
    stock_rets = [(sc[i] - sc[i - 1]) / sc[i - 1] for i in range(1, len(sc)) if sc[i - 1] != 0]
    bench_rets = [(bc[i] - bc[i - 1]) / bc[i - 1] for i in range(1, len(bc)) if bc[i - 1] != 0]

    n_rets = min(len(stock_rets), len(bench_rets))
    if n_rets < 10:
        return 1.0

    stock_rets = stock_rets[-n_rets:]
    bench_rets = bench_rets[-n_rets:]

    # Mean
    s_mean = sum(stock_rets) / n_rets
    b_mean = sum(bench_rets) / n_rets

    # Covariance and variance
    cov = sum(
        (s - s_mean) * (b - b_mean) for s, b in zip(stock_rets, bench_rets, strict=True)
    ) / n_rets
    var = sum((b - b_mean) ** 2 for b in bench_rets) / n_rets

    if var == 0:
        return 1.0

    return cov / var


async def load_cached_betas(data_dir: Path) -> dict[str, Any] | None:
    """Load cached beta data from the database.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    dict[str, Any] | None
        Cached beta data or None if not found.
    """
    raw = await db.get_setting(data_dir, _CACHE_KEY)
    if raw:
        return json.loads(raw)
    return None


async def save_betas(data_dir: Path, betas: dict[str, Any]) -> None:
    """Save beta data to the database cache.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    betas : dict[str, Any]
        Beta data to cache.
    """
    await db.set_setting(data_dir, _CACHE_KEY, json.dumps(betas))


async def refresh_betas(
    api_key: str,
    underlyings: set[str],
    data_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Compute fresh betas for all underlyings against SPY.

    Fetches 60-day daily bars from Massive and computes the regression.
    Results are cached in SQLite.

    Parameters
    ----------
    api_key : str
        Massive API key.
    underlyings : set[str]
        Ticker symbols to compute beta for.
    data_dir : Path
        Application data directory.
    client : httpx.AsyncClient | None
        Optional shared HTTP client.

    Returns
    -------
    dict[str, Any]
        Beta data including per-symbol betas, SPY price, and computation date.
    """
    logger.info("Computing betas for %s against %s", underlyings, _BENCHMARK)

    # Fetch SPY bars
    spy_bars = await massive.get_daily_bars(api_key, _BENCHMARK, days=60, client=client)
    if not spy_bars:
        logger.warning("No SPY bars returned — using default betas")
        return _default_betas(underlyings)

    spy_closes = [bar["c"] for bar in spy_bars]
    spy_price = spy_closes[-1] if spy_closes else 0.0

    betas: dict[str, float] = {}
    for ticker in underlyings:
        bars = await massive.get_daily_bars(api_key, ticker, days=60, client=client)
        if not bars:
            logger.warning("No bars for %s — defaulting beta to 1.0", ticker)
            betas[ticker] = 1.0
            continue

        closes = [bar["c"] for bar in bars]
        beta = compute_beta(closes, spy_closes)
        betas[ticker] = round(beta, 4)
        logger.info("Beta for %s: %.4f", ticker, beta)

    result = {
        "betas": betas,
        "spy_price": spy_price,
        "computed_at": datetime.date.today().isoformat(),
    }

    await save_betas(data_dir, result)
    return result


def _default_betas(underlyings: set[str]) -> dict[str, Any]:
    """Return default beta data when computation fails."""
    return {
        "betas": {t: 1.0 for t in underlyings},
        "spy_price": 0.0,
        "computed_at": "",
    }


def beta_weighted_delta(
    net_delta: float,
    underlying_price: float,
    beta: float,
    spy_price: float,
) -> float:
    """Compute beta-weighted delta in SPY-equivalent shares.

    Parameters
    ----------
    net_delta : float
        Raw net delta for the underlying.
    underlying_price : float
        Current price of the underlying.
    beta : float
        Beta of the underlying vs SPY.
    spy_price : float
        Current SPY price.

    Returns
    -------
    float
        Beta-weighted delta in SPY-equivalent shares.
    """
    if spy_price <= 0:
        return 0.0
    return net_delta * beta * (underlying_price / spy_price)
