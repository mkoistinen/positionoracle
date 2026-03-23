import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx

from positionoracle.massive import (
    StockWebSocket,
    get_option_contract_snapshot,
    get_stock_snapshot,
)


class TestGetOptionContractSnapshot:
    async def test_success(self):
        mock_response = httpx.Response(
            200,
            json={
                "results": {
                    "greeks": {"delta": -0.45},
                    "implied_volatility": 0.93,
                },
                "status": "OK",
            },
            request=httpx.Request("GET", "https://example.com"),
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        result = await get_option_contract_snapshot(
            "test-key", "AAPL", "O:AAPL251219C00150000", client=client,
        )
        assert result is not None
        assert result["greeks"]["delta"] == -0.45
        client.get.assert_awaited_once()

    async def test_http_error(self):
        mock_response = httpx.Response(
            403,
            json={"error": "forbidden"},
            request=httpx.Request("GET", "https://example.com"),
        )
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "forbidden", request=mock_response.request, response=mock_response,
            )
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        result = await get_option_contract_snapshot(
            "test-key", "AAPL", "O:AAPL251219C00150000", client=client,
        )
        assert result is None


class TestGetStockSnapshot:
    async def test_success(self):
        mock_response = httpx.Response(
            200,
            json={"ticker": {"price": 150.0, "symbol": "AAPL"}},
            request=httpx.Request("GET", "https://example.com"),
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        result = await get_stock_snapshot("test-key", "AAPL", client=client)
        assert result is not None
        assert result["price"] == 150.0

    async def test_http_error_returns_none(self):
        mock_response = httpx.Response(
            404,
            json={"error": "not found"},
            request=httpx.Request("GET", "https://example.com"),
        )
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "not found", request=mock_response.request, response=mock_response,
            )
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response)

        result = await get_stock_snapshot("test-key", "AAPL", client=client)
        assert result is None


class TestStockWebSocket:
    def test_init(self):
        ws = StockWebSocket(api_key="test-key")
        assert not ws._running
        assert ws._subscriptions == set()

    async def test_subscribe_tracks_tickers(self):
        ws = StockWebSocket(api_key="test-key")
        ws._ws = AsyncMock()
        await ws.subscribe({"AAPL", "MSFT"})
        assert "AAPL" in ws._subscriptions
        assert "MSFT" in ws._subscriptions

    async def test_unsubscribe_removes_tickers(self):
        ws = StockWebSocket(api_key="test-key")
        ws._ws = AsyncMock()
        ws._subscriptions = {"AAPL", "MSFT", "GOOG"}
        await ws.unsubscribe({"MSFT"})
        assert "MSFT" not in ws._subscriptions
        assert "AAPL" in ws._subscriptions

    async def test_disconnect(self):
        ws = StockWebSocket(api_key="test-key")
        mock_ws = AsyncMock()
        ws._ws = mock_ws
        ws._running = True

        # Create a real cancelled task
        async def noop():
            await asyncio.sleep(100)

        task = asyncio.create_task(noop())
        ws._task = task

        await ws.disconnect()
        assert not ws._running
        assert ws._ws is None
        assert ws._task is None
        mock_ws.close.assert_awaited_once()

    async def test_subscribe_no_ws(self):
        ws = StockWebSocket(api_key="test-key")
        await ws.subscribe({"AAPL"})
        assert "AAPL" in ws._subscriptions

    async def test_unsubscribe_no_ws(self):
        ws = StockWebSocket(api_key="test-key")
        ws._subscriptions = {"AAPL"}
        await ws.unsubscribe({"AAPL"})
        assert "AAPL" not in ws._subscriptions
