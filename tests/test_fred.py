"""Tests for FRED treasury-rate client."""

from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock

import httpx

from positionoracle import db, fred


class TestSeriesForDays:
    def test_short_dte_picks_1mo(self):
        # 7 DTE should choose DGS1MO (tenor 30) over DGS3MO (91).
        assert fred._series_for_days(7) == "DGS1MO"

    def test_medium_dte_picks_3mo(self):
        # 80 DTE is closer to 91 (DGS3MO) than to 30 (DGS1MO).
        assert fred._series_for_days(80) == "DGS3MO"

    def test_long_dte_picks_1y(self):
        assert fred._series_for_days(330) == "DGS1"


class TestFetchLatestObservation:
    async def test_picks_first_non_null(self):
        payload = {
            "observations": [
                {"date": "2099-01-03", "value": "."},
                {"date": "2099-01-02", "value": "4.25"},
                {"date": "2099-01-01", "value": "4.20"},
            ],
        }
        mock_response = httpx.Response(
            200, json=payload,
            request=httpx.Request("GET", "https://example.com"),
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        rate = await fred._fetch_latest_observation("test-key", "DGS1MO", client)
        assert rate == 0.0425

    async def test_all_null_returns_none(self):
        payload = {"observations": [{"value": "."}]}
        mock_response = httpx.Response(
            200, json=payload,
            request=httpx.Request("GET", "https://example.com"),
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)
        assert await fred._fetch_latest_observation("key", "DGS1MO", client) is None

    async def test_http_error_returns_none(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.HTTPError("boom"))
        assert await fred._fetch_latest_observation("key", "DGS1MO", client) is None


class TestRateCaching:
    async def test_load_cached_when_missing(self, data_dir):
        await db.init_db(data_dir)
        assert await fred.load_cached_rates(data_dir) is None

    async def test_round_trip_cache(self, data_dir):
        await db.init_db(data_dir)
        payload = {"rates": {"DGS1MO": 0.04}, "fetched_at": "2099-01-01"}
        await db.set_setting(data_dir, "fred_treasury_rates", json.dumps(payload))
        assert (await fred.load_cached_rates(data_dir)) == {"DGS1MO": 0.04}

    async def test_get_rate_uses_fallback_when_no_key(self, data_dir):
        await db.init_db(data_dir)
        rate = await fred.get_rate_for_dte("", data_dir, 30)
        # No key + no cache → fallback constant.
        assert rate == fred._FALLBACK_RATE

    async def test_get_rate_uses_cache_within_a_day(self, data_dir):
        await db.init_db(data_dir)
        # Seed a cache that's already "today" so no refresh happens.
        from datetime import date
        payload = {
            "rates": {"DGS1MO": math.log1p(0.05)},
            "fetched_at": date.today().isoformat(),
        }
        await db.set_setting(data_dir, "fred_treasury_rates", json.dumps(payload))

        rate = await fred.get_rate_for_dte("test-key", data_dir, 30)
        assert rate == math.log1p(0.05)
