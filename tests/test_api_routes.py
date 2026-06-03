"""Route tests for API key management and the public REST v1 endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import positionoracle.main as main_mod
from positionoracle import db
from positionoracle.main import (
    _COOKIE_NAME,
    _create_session_cookie,
    app,
    settings,
)


@pytest.fixture
def auth_cookie():
    return _create_session_cookie()


@pytest.fixture
async def client(tmp_path):
    settings.data_dir = tmp_path
    settings.secret_key = "test-secret-key"
    await db.init_db(tmp_path)

    # Reset main.py global state between tests.
    main_mod._positions = []
    main_mod._position_greeks.clear()
    main_mod._position_entries.clear()
    main_mod._underlying_prices.clear()
    main_mod._underlying_closes.clear()
    main_mod._blacklist = []
    main_mod._gex_profiles.clear()
    main_mod._beta_data = {}
    main_mod._last_report_generated = None

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


class TestKeyManagementAuth:
    async def test_create_requires_session(self, client):
        resp = await client.post("/api/keys", json={"name": "test"})
        assert resp.status_code == 401

    async def test_list_requires_session(self, client):
        resp = await client.get("/api/keys")
        assert resp.status_code == 401

    async def test_delete_requires_session(self, client):
        resp = await client.delete("/api/keys/1")
        assert resp.status_code == 401


class TestKeyManagementCRUD:
    async def test_create_returns_cleartext_once(self, client, auth_cookie):
        resp = await client.post(
            "/api/keys",
            json={"name": "test-key"},
            cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "test-key"
        assert body["key"].startswith("po_")
        assert body["key_prefix"] == body["key"][:8]
        assert "created_at" in body

    async def test_list_omits_cleartext(self, client, auth_cookie):
        await client.post(
            "/api/keys",
            json={"name": "alpha"},
            cookies={_COOKIE_NAME: auth_cookie},
        )
        resp = await client.get(
            "/api/keys", cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["keys"]) == 1
        # Cleartext must NEVER leak via list.
        assert "key" not in body["keys"][0]
        assert body["keys"][0]["name"] == "alpha"
        assert body["keys"][0]["key_prefix"].startswith("po_")

    async def test_delete_revokes(self, client, auth_cookie):
        create = await client.post(
            "/api/keys",
            json={"name": "revoke-me"},
            cookies={_COOKIE_NAME: auth_cookie},
        )
        key_id = create.json()["id"]

        delete = await client.delete(
            f"/api/keys/{key_id}", cookies={_COOKIE_NAME: auth_cookie},
        )
        assert delete.status_code == 200

        listing = await client.get(
            "/api/keys", cookies={_COOKIE_NAME: auth_cookie},
        )
        assert listing.json()["keys"] == []

    async def test_delete_unknown_returns_404(self, client, auth_cookie):
        resp = await client.delete(
            "/api/keys/9999", cookies={_COOKIE_NAME: auth_cookie},
        )
        assert resp.status_code == 404


class TestV1Auth:
    async def test_positions_requires_bearer(self, client):
        resp = await client.get("/api/v1/positions")
        assert resp.status_code == 401

    async def test_positions_rejects_invalid_bearer(self, client):
        resp = await client.get(
            "/api/v1/positions",
            headers={"Authorization": "Bearer po_invalidvalue"},
        )
        assert resp.status_code == 401

    async def test_washsale_requires_bearer(self, client):
        resp = await client.get("/api/v1/washsale")
        assert resp.status_code == 401

    async def test_revoked_key_returns_401(self, client, auth_cookie):
        create = await client.post(
            "/api/keys",
            json={"name": "ephemeral"},
            cookies={_COOKIE_NAME: auth_cookie},
        )
        cleartext = create.json()["key"]
        key_id = create.json()["id"]
        # Verify it works.
        good = await client.get(
            "/api/v1/positions",
            headers={"Authorization": f"Bearer {cleartext}"},
        )
        assert good.status_code == 200
        # Revoke it.
        await client.delete(
            f"/api/keys/{key_id}", cookies={_COOKIE_NAME: auth_cookie},
        )
        # Now it's rejected.
        after = await client.get(
            "/api/v1/positions",
            headers={"Authorization": f"Bearer {cleartext}"},
        )
        assert after.status_code == 401


class TestV1Positions:
    async def _make_key(self, client, auth_cookie) -> str:
        resp = await client.post(
            "/api/keys",
            json={"name": "test"},
            cookies={_COOKIE_NAME: auth_cookie},
        )
        return resp.json()["key"]

    async def test_returns_ws_compatible_shape(self, client, auth_cookie):
        cleartext = await self._make_key(client, auth_cookie)
        resp = await client.get(
            "/api/v1/positions",
            headers={"Authorization": f"Bearer {cleartext}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Top-level keys mirror the WebSocket update payload.
        assert body["type"] == "update"
        assert "last_updated" in body
        assert "market_open" in body
        assert "underlyings" in body
        assert "portfolio" in body
        # Empty by default (no positions seeded).
        assert body["underlyings"] == {}

    async def test_touches_last_used_at(self, client, auth_cookie):
        cleartext = await self._make_key(client, auth_cookie)
        # last_used_at starts null.
        listing_before = await client.get(
            "/api/keys", cookies={_COOKIE_NAME: auth_cookie},
        )
        assert listing_before.json()["keys"][0]["last_used_at"] is None

        # Make a call.
        await client.get(
            "/api/v1/positions",
            headers={"Authorization": f"Bearer {cleartext}"},
        )

        listing_after = await client.get(
            "/api/keys", cookies={_COOKIE_NAME: auth_cookie},
        )
        assert listing_after.json()["keys"][0]["last_used_at"] is not None


class TestV1Washsale:
    async def test_empty_blacklist(self, client, auth_cookie):
        create = await client.post(
            "/api/keys", json={"name": "k"},
            cookies={_COOKIE_NAME: auth_cookie},
        )
        cleartext = create.json()["key"]
        resp = await client.get(
            "/api/v1/washsale",
            headers={"Authorization": f"Bearer {cleartext}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == []
        assert body["last_report_generated"] is None


# ---------------------------------------------------------------------------
# v1 — Create / delete positions
# ---------------------------------------------------------------------------


async def _make_bearer(client, auth_cookie) -> str:
    """Mint an API key via the session-auth endpoint and return cleartext."""
    resp = await client.post(
        "/api/keys", json={"name": "test"},
        cookies={_COOKIE_NAME: auth_cookie},
    )
    return resp.json()["key"]


class TestV1CreatePositionValidation:
    async def test_requires_bearer(self, client):
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "put",
                "quantity": -1,
                "entry_time": "2026-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
                "strike": 150.0,
                "expiration": "2099-12-19",
            },
        )
        assert resp.status_code == 401

    async def test_option_missing_strike_400(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "put",
                "quantity": -1,
                "entry_time": "2026-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
                "expiration": "2099-12-19",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 400

    async def test_expired_contract_rejected(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "put",
                "quantity": -1,
                "entry_time": "2020-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
                "strike": 150.0,
                "expiration": "2020-12-19",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 400

    async def test_zero_quantity_rejected(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "put",
                "quantity": 0,
                "entry_time": "2026-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
                "strike": 150.0,
                "expiration": "2099-12-19",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 400

    async def test_invalid_contract_type_422(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "spread",
                "quantity": 1,
                "entry_time": "2026-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 422


class TestV1CreatePositionInsert:
    @pytest.fixture(autouse=True)
    def _mock_external_services(self, monkeypatch):
        """Stub external calls used by the entry-data backfill + snapshot loop."""
        from positionoracle import fred, massive
        from positionoracle import main as m

        # Use a stable bar that will match any entry_time.
        async def fake_minute_bars(*_args, **_kwargs):
            return [{"t": 0, "h": 101.0, "l": 99.0, "o": 100.0, "c": 100.0}]

        def fake_pick_bar(bars, _target_ms, **_kwargs):
            return bars[0] if bars else None

        async def fake_rate(*_args, **_kwargs):
            return 0.04

        # Skip the snapshot refresh — it would otherwise hit Massive for
        # Greeks/quotes. The insert path itself still computes entry IV.
        async def fake_refresh():
            return None

        monkeypatch.setattr(massive, "get_minute_bars", fake_minute_bars)
        monkeypatch.setattr(massive, "pick_bar_for_minute", fake_pick_bar)
        monkeypatch.setattr(fred, "get_rate_for_dte", fake_rate)
        monkeypatch.setattr(m, "_refresh_options_snapshots", fake_refresh)
        # The insert path runs `_ensure_market_data` which would start
        # a WebSocket — stub it.
        monkeypatch.setattr(m, "_ensure_market_data", fake_refresh)
        # Massive API key needs to be truthy for the snapshot-refresh
        # branch and for the entry-IV computation to fire.
        monkeypatch.setattr(m.settings, "massive_api_key", "test-key")

    async def test_create_short_put_returns_entry_data(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "put",
                "quantity": -1,
                "entry_time": "2099-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
                "strike": 95.0,
                "expiration": "2099-12-19",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["symbol"].startswith("AAPL  ")
        assert body["contract_type"] == "put"
        assert body["entry_spot"] == 100.0  # (101 + 99) / 2
        assert body["entry_premium_per_share"] == 2.0
        # IV inverted from the BS price should be a sane positive number.
        assert body["entry_iv"] is not None
        assert 0 < body["entry_iv"] < 5.0
        assert body["entry_rate"] == 0.04

    async def test_create_stock_skips_entry_iv(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "stock",
                "quantity": 100,
                "entry_time": "2099-06-02T14:30:00-04:00",
                "entry_premium_per_share": 150.0,
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["symbol"] == "AAPL"
        assert body["contract_type"] == "stock"
        assert body["entry_iv"] is None

    async def test_duplicate_symbol_returns_409(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        payload = {
            "underlying": "AAPL",
            "contract_type": "put",
            "quantity": -1,
            "entry_time": "2099-06-02T14:30:00-04:00",
            "entry_premium_per_share": 2.0,
            "strike": 95.0,
            "expiration": "2099-12-19",
        }
        first = await client.post(
            "/api/v1/positions",
            json=payload,
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/v1/positions",
            json=payload,
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert second.status_code == 409

    async def test_position_appears_in_get_positions(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "stock",
                "quantity": 100,
                "entry_time": "2099-06-02T14:30:00-04:00",
                "entry_premium_per_share": 150.0,
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        listing = await client.get(
            "/api/v1/positions",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert listing.status_code == 200
        underlyings = listing.json()["underlyings"]
        assert "AAPL" in underlyings


class TestV1DeletePosition:
    async def test_requires_bearer(self, client):
        resp = await client.delete("/api/v1/positions/SOMETHING")
        assert resp.status_code == 401

    async def test_unknown_symbol_returns_404(self, client, auth_cookie):
        bearer = await _make_bearer(client, auth_cookie)
        resp = await client.delete(
            "/api/v1/positions/NOPE",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert resp.status_code == 404

    async def test_delete_round_trip(self, client, auth_cookie, monkeypatch):
        from positionoracle import fred, massive
        from positionoracle import main as m

        async def fake_minute_bars(*_args, **_kwargs):
            return [{"t": 0, "h": 101.0, "l": 99.0, "o": 100.0, "c": 100.0}]

        def fake_pick_bar(bars, _target_ms, **_kwargs):
            return bars[0] if bars else None

        async def fake_rate(*_args, **_kwargs):
            return 0.04

        async def noop():
            return None

        monkeypatch.setattr(massive, "get_minute_bars", fake_minute_bars)
        monkeypatch.setattr(massive, "pick_bar_for_minute", fake_pick_bar)
        monkeypatch.setattr(fred, "get_rate_for_dte", fake_rate)
        monkeypatch.setattr(m, "_refresh_options_snapshots", noop)
        monkeypatch.setattr(m, "_ensure_market_data", noop)
        monkeypatch.setattr(m.settings, "massive_api_key", "test-key")

        bearer = await _make_bearer(client, auth_cookie)
        create = await client.post(
            "/api/v1/positions",
            json={
                "underlying": "AAPL",
                "contract_type": "put",
                "quantity": -1,
                "entry_time": "2099-06-02T14:30:00-04:00",
                "entry_premium_per_share": 2.0,
                "strike": 95.0,
                "expiration": "2099-12-19",
            },
            headers={"Authorization": f"Bearer {bearer}"},
        )
        symbol = create.json()["symbol"]

        delete = await client.delete(
            f"/api/v1/positions/{symbol}",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert delete.status_code == 204

        # Position is gone from the list and entry data is pruned.
        listing = await client.get(
            "/api/v1/positions",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert listing.json()["underlyings"] == {}
