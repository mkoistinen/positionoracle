"""Tests for VRP=1.0 trade-planning quotes."""

from __future__ import annotations

import pytest

from positionoracle import planner, vrp
from positionoracle.types import ContractType


class TestPriceQuote:
    def test_fair_price_matches_bs_at_rv(self):
        # The VRP=1.0 fair price is just BS priced at sigma = RV.
        q = planner.price_quote(
            spot=100.0, strike=100.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.CALL, direction="long",
        )
        expected = vrp.bs_price(
            s=100.0, k=100.0, t=30 / 365.0, r=0.04, sigma=0.25,
            contract_type=ContractType.CALL,
        )
        assert q.fair_price == pytest.approx(expected)
        assert q.fair_price_contract == pytest.approx(expected * 100)

    def test_no_live_iv_is_na(self):
        q = planner.price_quote(
            spot=100.0, strike=95.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.PUT, direction="short",
        )
        assert q.current_vrp is None
        assert q.signal == "na"
        assert "fair value" in q.verdict.lower()

    def test_short_favors_rich_iv(self):
        # IV (0.35) well above RV (0.25) -> VRP ~0.71 -> rich -> good short.
        q = planner.price_quote(
            spot=100.0, strike=95.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.PUT, direction="short",
            live_iv=0.35, live_mid=2.5,
        )
        assert q.current_vrp == pytest.approx(0.25 / 0.35)
        assert q.signal == "favorable"

    def test_long_dislikes_rich_iv(self):
        # Same rich IV is unfavorable for a long (you'd overpay for vol).
        q = planner.price_quote(
            spot=100.0, strike=95.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.PUT, direction="long",
            live_iv=0.35, live_mid=2.5,
        )
        assert q.signal == "unfavorable"

    def test_long_favors_cheap_iv(self):
        # IV (0.20) below RV (0.25) -> VRP 1.25 -> cheap -> good long.
        q = planner.price_quote(
            spot=100.0, strike=105.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.CALL, direction="long",
            live_iv=0.20, live_mid=1.1,
        )
        assert q.current_vrp == pytest.approx(0.25 / 0.20)
        assert q.signal == "favorable"

    def test_short_dislikes_cheap_iv(self):
        q = planner.price_quote(
            spot=100.0, strike=105.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.CALL, direction="short",
            live_iv=0.20, live_mid=1.1,
        )
        assert q.signal == "unfavorable"

    def test_within_band_is_neutral(self):
        # IV within ±5% of RV -> neutral for either side.
        q = planner.price_quote(
            spot=100.0, strike=100.0, dte_days=30, rate=0.04, rv=0.25,
            contract_type=ContractType.CALL, direction="short",
            live_iv=0.25, live_mid=1.5,
        )
        assert q.signal == "neutral"

    def test_expired_contract_floors_clock(self):
        # dte_days <= 0 must not divide-by-zero; BS clock is floored.
        q = planner.price_quote(
            spot=100.0, strike=100.0, dte_days=0, rate=0.04, rv=0.25,
            contract_type=ContractType.CALL, direction="long",
        )
        assert q.fair_price >= 0.0


class TestComputePricePlan:
    async def test_prices_entered_and_scan(self, monkeypatch):
        import datetime

        from positionoracle import fred, main, massive
        from positionoracle.api_models import PriceOptionRequest

        exp = datetime.date.today() + datetime.timedelta(days=30)
        exp_iso = exp.isoformat()

        async def fake_daily_bars(*_a, **_k):
            return [{"c": 100.0 + (i % 3) * 0.5} for i in range(30)]

        async def fake_rate(*_a, **_k):
            return 0.04

        def _contract(strike, iv, bid, ask):
            return {
                "details": {
                    "strike_price": strike,
                    "contract_type": "put",
                    "expiration_date": exp_iso,
                },
                "implied_volatility": iv,
                "last_quote": {"bid": bid, "ask": ask},
                "underlying_asset": {"price": 101.0},
            }

        async def fake_chain(*_a, **_k):
            return [
                _contract(95.0, 0.30, 1.0, 1.2),
                _contract(100.0, 0.28, 2.0, 2.2),
                _contract(105.0, 0.26, 3.5, 3.7),
            ]

        monkeypatch.setattr(main.settings, "massive_api_key", "test-key")
        monkeypatch.setattr(massive, "get_daily_bars", fake_daily_bars)
        monkeypatch.setattr(massive, "get_options_chain_snapshot", fake_chain)
        monkeypatch.setattr(fred, "get_rate_for_dte", fake_rate)

        body = PriceOptionRequest(
            underlying="aapl", contract_type="put", direction="short",
            strike=100.0, expiration=exp,
        )
        resp = await main._compute_price_plan(body)

        assert resp.underlying == "AAPL"
        assert resp.spot == pytest.approx(101.0)
        assert resp.dte_days == 30
        assert resp.entered.strike == pytest.approx(100.0)
        assert resp.entered.is_entered
        assert resp.entered.fair_price > 0
        assert resp.entered.current_vrp is not None
        assert len(resp.scan) == 3
        assert sum(1 for q in resp.scan if q.is_entered) == 1

    async def test_missing_massive_key_raises(self, monkeypatch):
        import datetime

        from fastapi import HTTPException

        from positionoracle import main
        from positionoracle.api_models import PriceOptionRequest

        monkeypatch.setattr(main.settings, "massive_api_key", "")
        body = PriceOptionRequest(
            underlying="AAPL", contract_type="put", direction="short",
            strike=100.0,
            expiration=datetime.date.today() + datetime.timedelta(days=10),
        )
        with pytest.raises(HTTPException):
            await main._compute_price_plan(body)
