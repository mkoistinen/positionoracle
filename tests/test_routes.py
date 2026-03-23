import datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import positionoracle.main as main_mod
from positionoracle.main import (
    _COOKIE_NAME,
    _create_session_cookie,
    _serialize_summaries,
    _verify_session,
    app,
    settings,
)
from positionoracle.types import (
    Advice,
    AdviceLevel,
    ContractType,
    Greeks,
    PortfolioSummary,
    Position,
    PositionGreeks,
)


@pytest.fixture
def auth_cookie():
    return _create_session_cookie()


@pytest.fixture
async def client(tmp_path):
    settings.data_dir = tmp_path
    settings.secret_key = "test-secret-key"
    settings.setup_token = "test-setup-token"

    from positionoracle import db
    await db.init_db(tmp_path)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


def _make_position(symbol="AAPL251219C00150000", underlying="AAPL"):
    return Position(
        symbol=symbol,
        underlying=underlying,
        contract_type=ContractType.CALL,
        strike=150.0,
        expiration=datetime.date(2025, 12, 19),
        quantity=10,
        cost_basis=5000.0,
    )


class TestSessionHelpers:
    def test_create_and_verify_session(self):
        cookie = _create_session_cookie()
        assert _verify_session(cookie)

    def test_verify_none(self):
        assert not _verify_session(None)

    def test_verify_invalid(self):
        assert not _verify_session("garbage")


class TestAuthRoutes:
    async def test_auth_status_unauthenticated(self, client):
        resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert not data["authenticated"]
        assert not data["has_credentials"]

    async def test_auth_status_authenticated(self, client, auth_cookie):
        resp = await client.get(
            "/api/auth/status",
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"]

    async def test_register_begin_no_token(self, client):
        resp = await client.post("/api/auth/register/begin")
        assert resp.status_code == 403

    async def test_register_begin_with_token(self, client):
        resp = await client.post(
            "/api/auth/register/begin?setup_token=test-setup-token",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "options" in data
        assert "challenge_token" in data

    async def test_login_begin_no_credentials(self, client):
        resp = await client.post("/api/auth/login/begin")
        assert resp.status_code == 404

    async def test_logout(self, client, auth_cookie):
        resp = await client.post(
            "/api/auth/logout",
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 200

    async def test_login_complete_bad_credential(self, client, tmp_path):
        from positionoracle.auth import save_credentials
        save_credentials(tmp_path, [
            {
                "id": "dGVzdA",
                "public_key": "dGVzdA",
                "sign_count": 0,
                "name": "Key",
                "registered_at": "2025-01-01T00:00:00",
            },
        ])
        # Begin to get a valid challenge
        resp = await client.post("/api/auth/login/begin")
        assert resp.status_code == 200
        token = resp.json()["challenge_token"]

        # Complete with garbage credential
        resp = await client.post(
            "/api/auth/login/complete",
            json={
                "credential": {"id": "bad", "rawId": "bad"},
                "challenge_token": token,
            },
        )
        assert resp.status_code == 401

    async def test_register_complete_bad_credential(self, client):
        resp = await client.post(
            "/api/auth/register/begin?setup_token=test-setup-token",
        )
        token = resp.json()["challenge_token"]

        resp = await client.post(
            "/api/auth/register/complete",
            json={
                "credential": {"id": "bad", "rawId": "bad"},
                "challenge_token": token,
                "name": "Test",
            },
        )
        assert resp.status_code == 400


class TestPositionRoutes:
    async def test_list_positions_unauthenticated(self, client):
        resp = await client.get("/api/positions")
        assert resp.status_code == 401

    async def test_list_positions_empty(self, client, auth_cookie):
        resp = await client.get(
            "/api/positions",
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["positions"] == []

    async def test_import_unauthenticated(self, client):
        resp = await client.post(
            "/api/positions/import",
            files={"file": ("test.xml", b"<root/>", "application/xml")},
        )
        assert resp.status_code == 401

    async def test_import_empty_xml(self, client, auth_cookie):
        resp = await client.post(
            "/api/positions/import",
            files={
                "file": ("test.xml", b"<root/>", "application/xml"),
            },
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 400

    async def test_import_valid_flex(self, client, auth_cookie):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Test" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567">
      <OpenPositions>
        <OpenPosition
          assetCategory="OPT"
          symbol="AAPL251219C00150000"
          underlyingSymbol="AAPL"
          putCall="C"
          strike="150"
          expiry="20251219"
          position="10"
          costBasisMoney="5000.00"
          multiplier="100"
        />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

        with patch(
            "positionoracle.main._ensure_market_data",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/api/positions/import",
                files={
                    "file": (
                        "test.xml",
                        xml.encode(),
                        "application/xml",
                    ),
                },
                cookies={_COOKIE_NAME: auth_cookie},
            )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 1

        resp = await client.get(
            "/api/positions",
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert len(resp.json()["positions"]) == 1

    async def test_delete_position_not_found(self, client, auth_cookie):
        resp = await client.delete(
            "/api/positions/NOSUCH",
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 404

    async def test_clear_positions(self, client, auth_cookie):
        resp = await client.delete(
            "/api/positions",
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 200


class TestSerializeSummaries:
    def test_serialize_empty(self):
        result = _serialize_summaries({})
        assert result["type"] == "update"
        assert result["underlyings"] == {}

    def test_serialize_with_data(self):
        pos = _make_position(symbol="TEST", underlying="TEST")
        pg = PositionGreeks(
            position=pos,
            greeks=Greeks(delta=0.5, gamma=0.04),
            underlying_price=100.0,
        )
        summary = PortfolioSummary(
            underlying="TEST",
            net_delta=50.0,
            positions=[pg],
            advice=[
                Advice(
                    level=AdviceLevel.WARNING,
                    message="Test",
                    position_symbol="TEST",
                    metric="delta",
                    value=0.5,
                    threshold=0.3,
                )
            ],
        )

        result = _serialize_summaries({"TEST": summary})
        assert "TEST" in result["underlyings"]
        data = result["underlyings"]["TEST"]
        assert data["net_delta"] == 50.0
        assert len(data["positions"]) == 1
        assert len(data["advice"]) == 1
        assert data["advice"][0]["level"] == "warning"


class TestMarketDataOrchestration:
    async def test_on_trade_updates_price(self, tmp_path):
        settings.data_dir = tmp_path
        from positionoracle import db
        await db.init_db(tmp_path)

        main_mod._underlying_prices.clear()
        main_mod._position_greeks.clear()

        await main_mod._on_trade("AAPL", 155.0)
        assert main_mod._underlying_prices["AAPL"] == 155.0

    async def test_recompute_updates_underlying_price(self, tmp_path):
        settings.data_dir = tmp_path
        from positionoracle import db
        await db.init_db(tmp_path)

        pos = _make_position()
        main_mod._positions.clear()
        main_mod._positions.append(pos)
        main_mod._underlying_prices["AAPL"] = 160.0
        main_mod._position_greeks[pos.symbol] = PositionGreeks(
            position=pos,
            greeks=Greeks(delta=0.5),
            underlying_price=150.0,
        )

        await main_mod._recompute_positions("AAPL")
        pg = main_mod._position_greeks[pos.symbol]
        assert pg.underlying_price == 160.0

        # Clean up
        main_mod._positions.clear()
        main_mod._position_greeks.clear()
        main_mod._underlying_prices.clear()

    async def test_refresh_snapshots_skips_without_key(self, tmp_path):
        settings.data_dir = tmp_path
        settings.massive_api_key = ""
        pos = _make_position()
        main_mod._positions.clear()
        main_mod._positions.append(pos)

        # Should return early without error
        await main_mod._refresh_options_snapshots()

        main_mod._positions.clear()
        settings.massive_api_key = ""

    async def test_refresh_snapshots_skips_without_positions(
        self, tmp_path,
    ):
        settings.data_dir = tmp_path
        settings.massive_api_key = "test-key"
        main_mod._positions.clear()

        await main_mod._refresh_options_snapshots()

        settings.massive_api_key = ""

    async def test_refresh_with_snapshot_data(self, tmp_path):
        settings.data_dir = tmp_path
        settings.massive_api_key = "test-key"
        from positionoracle import db
        await db.init_db(tmp_path)

        pos = _make_position()
        main_mod._positions.clear()
        main_mod._positions.append(pos)
        main_mod._position_greeks.clear()
        main_mod._underlying_prices["AAPL"] = 155.0

        snapshot = {
            "greeks": {
                "delta": 0.55,
                "gamma": 0.04,
                "theta": -0.03,
                "vega": 0.15,
            },
            "implied_volatility": 0.25,
            "underlying_asset": {"price": 155.0},
            "last_quote": {"bid": 5.0, "ask": 5.2},
        }

        with patch(
            "positionoracle.main.massive.get_option_contract_snapshot",
            new_callable=AsyncMock,
            return_value=snapshot,
        ):
            await main_mod._refresh_options_snapshots()

        assert pos.symbol in main_mod._position_greeks
        pg = main_mod._position_greeks[pos.symbol]
        assert pg.greeks.delta == 0.55
        assert pg.underlying_price == 155.0
        assert pg.option_mid == pytest.approx(5.1)

        # Clean up
        main_mod._positions.clear()
        main_mod._position_greeks.clear()
        main_mod._underlying_prices.clear()
        settings.massive_api_key = ""

    async def test_refresh_no_snapshot_match(self, tmp_path):
        settings.data_dir = tmp_path
        settings.massive_api_key = "test-key"
        from positionoracle import db
        await db.init_db(tmp_path)

        pos = _make_position()
        main_mod._positions.clear()
        main_mod._positions.append(pos)
        main_mod._position_greeks.clear()
        main_mod._underlying_prices["AAPL"] = 150.0

        with patch(
            "positionoracle.main.massive.get_option_contract_snapshot",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await main_mod._refresh_options_snapshots()

        # Should create placeholder
        assert pos.symbol in main_mod._position_greeks
        assert main_mod._position_greeks[pos.symbol].greeks.delta == 0.0

        # Clean up
        main_mod._positions.clear()
        main_mod._position_greeks.clear()
        settings.massive_api_key = ""


class TestEnsureMarketData:
    async def test_no_api_key(self):
        settings.massive_api_key = ""
        main_mod._positions.clear()
        main_mod._positions.append(_make_position())
        await main_mod._ensure_market_data()
        assert main_mod.stock_ws is None
        main_mod._positions.clear()

    async def test_no_positions(self):
        settings.massive_api_key = "test-key"
        main_mod._positions.clear()
        await main_mod._ensure_market_data()
        assert main_mod.stock_ws is None
        settings.massive_api_key = ""


class TestStopMarketData:
    async def test_stop_cleans_up(self):
        mock_ws = AsyncMock()
        main_mod.stock_ws = mock_ws

        mock_task = AsyncMock()
        mock_task.cancel = lambda: None
        mock_task.done = lambda: False
        main_mod._snapshot_task = mock_task

        await main_mod._stop_market_data()
        assert main_mod.stock_ws is None
        assert main_mod._snapshot_task is None
        mock_ws.disconnect.assert_awaited_once()

    async def test_stop_noop_when_not_running(self):
        main_mod.stock_ws = None
        main_mod._snapshot_task = None
        await main_mod._stop_market_data()


class TestFrontendFallback:
    async def test_missing_frontend(self, client):
        resp = await client.get("/nonexistent")
        assert resp.status_code == 404
