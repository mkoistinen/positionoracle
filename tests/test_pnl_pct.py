"""Tests for direction-aware P&L% and BS theoretical mid computation."""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from positionoracle import main
from positionoracle.types import (
    ContractType,
    Greeks,
    Position,
    PositionEntry,
    PositionGreeks,
)
from positionoracle.vrp import bs_price


def _make_position(
    *, symbol: str = "TEST  991219P00100000",
    underlying: str = "TEST",
    contract_type: ContractType = ContractType.PUT,
    strike: float = 100.0,
    quantity: int = -1,
    multiplier: int = 100,
    cost_basis: float = -200.0,  # $2 received per share for a short
    days_to_expiry: int = 30,
) -> Position:
    return Position(
        symbol=symbol,
        underlying=underlying,
        contract_type=contract_type,
        strike=strike,
        expiration=datetime.date.today() + datetime.timedelta(days=days_to_expiry),
        quantity=quantity,
        cost_basis=cost_basis,
        multiplier=multiplier,
    )


def _make_entry(
    pos: Position,
    *,
    entry_premium_per_share: float = 2.0,
    entry_iv: float | None = 0.25,
    entry_rate: float = 0.04,
) -> PositionEntry:
    return PositionEntry(
        symbol=pos.symbol,
        underlying=pos.underlying,
        entry_time=datetime.datetime.now(tz=datetime.UTC)
        - datetime.timedelta(days=15),
        entry_spot=100.0,
        entry_premium_per_share=entry_premium_per_share,
        entry_iv=entry_iv,
        entry_rate=entry_rate,
        computed_at=datetime.datetime.now(tz=datetime.UTC),
    )


def _make_pg(pos: Position, *, spot: float = 100.0, iv: float = 0.25) -> PositionGreeks:
    return PositionGreeks(
        position=pos,
        greeks=Greeks(implied_volatility=iv),
        underlying_price=spot,
    )


@pytest.fixture
def clean_caches():
    """Reset the in-memory caches main.py reads from."""
    main._position_entries.clear()
    main._underlying_closes.clear()
    yield
    main._position_entries.clear()
    main._underlying_closes.clear()


class TestTheoreticalMid:
    def test_matches_bs_price_with_live_iv(self, clean_caches):
        pos = _make_position(days_to_expiry=30)
        entry = _make_entry(pos, entry_rate=0.04)
        main._position_entries[pos.symbol] = entry

        pg = _make_pg(pos, spot=105.0, iv=0.30)
        main._apply_derived_metrics_to_position(pg)

        expected = bs_price(
            s=105.0, k=100.0, t=30 / 365.0, r=0.04, sigma=0.30,
            contract_type=ContractType.PUT,
        )
        assert pg.theoretical_mid == pytest.approx(expected, rel=1e-6)

    def test_falls_back_to_default_rate_when_no_entry(self, clean_caches):
        pos = _make_position(days_to_expiry=30)
        pg = _make_pg(pos, spot=100.0, iv=0.25)
        main._apply_derived_metrics_to_position(pg)
        # Theoretical mid still computed using the 0.05 fallback rate.
        expected = bs_price(
            s=100.0, k=100.0, t=30 / 365.0, r=0.05, sigma=0.25,
            contract_type=ContractType.PUT,
        )
        assert pg.theoretical_mid == pytest.approx(expected, rel=1e-6)

    def test_none_when_iv_missing(self, clean_caches):
        pos = _make_position()
        pg = _make_pg(pos, spot=100.0, iv=0.0)
        main._apply_derived_metrics_to_position(pg)
        assert pg.theoretical_mid is None

    def test_none_when_expired(self, clean_caches):
        pos = _make_position(days_to_expiry=-1)
        pg = _make_pg(pos, spot=100.0, iv=0.25)
        main._apply_derived_metrics_to_position(pg)
        assert pg.theoretical_mid is None


class TestPnlPct:
    def test_short_decayed_position_is_positive(self, clean_caches):
        # Sold at $2.00, theoretical mid will be ~$0.50 with these params.
        pos = _make_position(
            contract_type=ContractType.PUT, strike=90.0, days_to_expiry=10,
        )
        main._position_entries[pos.symbol] = _make_entry(
            pos, entry_premium_per_share=2.0,
        )
        pg = _make_pg(pos, spot=105.0, iv=0.15)  # well OTM, low vol
        main._apply_derived_metrics_to_position(pg)

        assert pg.pnl_pct is not None
        assert pg.pnl_pct > 0.5  # earned most of it

    def test_short_at_a_loss_is_negative(self, clean_caches):
        # Sold a put at $1, underlying tanked, now worth $5.
        pos = _make_position(
            contract_type=ContractType.PUT, strike=100.0, days_to_expiry=30,
        )
        main._position_entries[pos.symbol] = _make_entry(
            pos, entry_premium_per_share=1.0,
        )
        pg = _make_pg(pos, spot=92.0, iv=0.55)  # spot below strike, high IV
        main._apply_derived_metrics_to_position(pg)

        assert pg.pnl_pct is not None
        assert pg.pnl_pct < 0

    def test_long_profitable_position_is_positive(self, clean_caches):
        # Bought a call at $1 ATM, underlying ran up.
        pos = _make_position(
            contract_type=ContractType.CALL,
            strike=100.0,
            quantity=1,
            cost_basis=100.0,
            days_to_expiry=20,
        )
        main._position_entries[pos.symbol] = _make_entry(
            pos, entry_premium_per_share=1.0,
        )
        pg = _make_pg(pos, spot=110.0, iv=0.25)  # deep ITM
        main._apply_derived_metrics_to_position(pg)

        assert pg.pnl_pct is not None
        assert pg.pnl_pct > 0

    def test_returns_none_without_entry(self, clean_caches):
        pos = _make_position()
        pg = _make_pg(pos)
        main._apply_derived_metrics_to_position(pg)
        assert pg.pnl_pct is None

    def test_stock_theoretical_mid_is_none(self, clean_caches):
        pos = _make_position(
            symbol="AAPL", underlying="AAPL", contract_type=ContractType.STOCK,
            strike=0.0, multiplier=1, quantity=100, cost_basis=15000.0,
        )
        pg = _make_pg(pos, spot=160.0)
        main._apply_derived_metrics_to_position(pg)
        # Theoretical mid only applies to options.
        assert pg.theoretical_mid is None

    def test_stock_long_in_profit(self, clean_caches):
        # Bought 100 AAPL @ $150 (cost basis 15000), now at $165.
        pos = _make_position(
            symbol="AAPL", underlying="AAPL", contract_type=ContractType.STOCK,
            strike=0.0, multiplier=1, quantity=100, cost_basis=15000.0,
        )
        pg = _make_pg(pos, spot=165.0)
        main._apply_derived_metrics_to_position(pg)
        # (165 - 150) / 150 = 0.10
        assert pg.pnl_pct == pytest.approx(0.10)

    def test_stock_long_at_a_loss(self, clean_caches):
        # Bought 100 AAPL @ $150, now at $135.
        pos = _make_position(
            symbol="AAPL", underlying="AAPL", contract_type=ContractType.STOCK,
            strike=0.0, multiplier=1, quantity=100, cost_basis=15000.0,
        )
        pg = _make_pg(pos, spot=135.0)
        main._apply_derived_metrics_to_position(pg)
        # (135 - 150) / 150 = -0.10
        assert pg.pnl_pct == pytest.approx(-0.10)

    def test_stock_short_in_profit(self, clean_caches):
        # Sold short 100 AAPL @ $150 (proceeds -15000), now at $135 → covered cheaper.
        pos = _make_position(
            symbol="AAPL", underlying="AAPL", contract_type=ContractType.STOCK,
            strike=0.0, multiplier=1, quantity=-100, cost_basis=-15000.0,
        )
        pg = _make_pg(pos, spot=135.0)
        main._apply_derived_metrics_to_position(pg)
        # Short: (150 - 135) / 150 = 0.10
        assert pg.pnl_pct == pytest.approx(0.10)

    def test_stock_short_at_a_loss(self, clean_caches):
        # Sold short 100 AAPL @ $150, now at $165 → covering more expensive.
        pos = _make_position(
            symbol="AAPL", underlying="AAPL", contract_type=ContractType.STOCK,
            strike=0.0, multiplier=1, quantity=-100, cost_basis=-15000.0,
        )
        pg = _make_pg(pos, spot=165.0)
        main._apply_derived_metrics_to_position(pg)
        # Short: (150 - 165) / 150 = -0.10
        assert pg.pnl_pct == pytest.approx(-0.10)

    def test_stock_pnl_none_without_underlying_price(self, clean_caches):
        # Stock position but no live price yet.
        pos = _make_position(
            symbol="AAPL", underlying="AAPL", contract_type=ContractType.STOCK,
            strike=0.0, multiplier=1, quantity=100, cost_basis=15000.0,
        )
        pg = _make_pg(pos, spot=0.0)
        main._apply_derived_metrics_to_position(pg)
        assert pg.pnl_pct is None

    def test_short_at_entry_price_is_zero(self, clean_caches):
        # Pin theoretical mid to entry premium → pnl_pct should be 0.
        pos = _make_position(days_to_expiry=30)
        main._position_entries[pos.symbol] = _make_entry(
            pos, entry_premium_per_share=2.0,
        )
        pg = _make_pg(pos, spot=100.0, iv=0.25)

        # Force theoretical_mid to equal entry premium.
        with patch("positionoracle.vrp.bs_price", return_value=2.0):
            main._apply_derived_metrics_to_position(pg)

        assert pg.pnl_pct == pytest.approx(0.0)
