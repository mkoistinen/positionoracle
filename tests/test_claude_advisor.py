"""Tests for the Claude advisor context formatter."""

from __future__ import annotations

from positionoracle.claude_advisor import (
    _fmt_pnl_pct,
    _fmt_vrp,
    _format_position_context,
)


class TestFormatPnlPct:
    def test_positive(self):
        assert _fmt_pnl_pct(0.75) == "+75%"

    def test_negative(self):
        assert _fmt_pnl_pct(-0.32) == "-32%"

    def test_zero(self):
        assert _fmt_pnl_pct(0.0) == "+0%"

    def test_none(self):
        assert _fmt_pnl_pct(None) == "—"


class TestFormatVrp:
    def test_fully_populated(self):
        s = _fmt_vrp(0.82, 0.30, 0.245, 21)
        assert s == "0.82 (entry IV 30.0% vs 21d RV 24.5%)"

    def test_missing_vrp_but_entry_present(self):
        # Entry IV known but RV not yet computable.
        assert _fmt_vrp(None, 0.30, None, 0) == "— (insufficient realized-vol data)"

    def test_missing_entry_iv(self):
        # Entry not yet backfilled.
        assert _fmt_vrp(None, None, None, 0) == "— (entry IV pending)"


class TestFormatPositionContext:
    def _summary(self, **pos_overrides) -> dict:
        """Build a one-position summary for an underlying."""
        default_pos = {
            "symbol": "AAPL  991219P00150000",
            "underlying": "AAPL",
            "contract_type": "put",
            "strike": 150.0,
            "expiration": "2099-12-19",
            "quantity": -1,
            "cost_basis": -200.0,
            "multiplier": 100,
            "underlying_price": 155.0,
            "option_mid": None,
            "theoretical_mid": 0.50,
            "pnl_pct": 0.75,
            "vrp": 0.82,
            "entry_iv": 0.30,
            "rv": 0.245,
            "rv_window_days": 21,
            "greeks": {
                "delta": -0.10,
                "gamma": 0.01,
                "theta": -0.02,
                "vega": 0.05,
                "vanna": 0.001,
                "charm": 0.0005,
                "vomma": 0.002,
                "implied_volatility": 0.25,
            },
        }
        default_pos.update(pos_overrides)
        return {
            "net_delta": -10.0,
            "net_gamma": 1.0,
            "net_theta": -2.0,
            "net_vega": 5.0,
            "positions": [default_pos],
        }

    def test_includes_pnl_and_vrp_lines(self):
        out = _format_position_context(
            "AAPL", self._summary(), spot_price=155.0,
            beta=1.2, beta_weighted_delta=-12.0,
        )
        assert "P&L +75%" in out
        assert "VRP 0.82" in out
        assert "entry IV 30.0%" in out
        assert "21d RV 24.5%" in out

    def test_dashes_when_metrics_missing(self):
        out = _format_position_context(
            "AAPL",
            self._summary(pnl_pct=None, vrp=None, entry_iv=None, rv=None,
                          rv_window_days=0),
            spot_price=155.0, beta=1.2, beta_weighted_delta=-12.0,
        )
        assert "P&L —" in out
        assert "VRP — (entry IV pending)" in out

    def test_stock_lines_have_no_pnl_vrp(self):
        stock_summary = {
            "net_delta": 100.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "positions": [{
                "symbol": "AAPL",
                "underlying": "AAPL",
                "contract_type": "stock",
                "strike": 0.0,
                "expiration": "9999-12-31",
                "quantity": 100,
                "cost_basis": 15000.0,
                "multiplier": 1,
                "underlying_price": 155.0,
                "option_mid": None,
                "theoretical_mid": None,
                "pnl_pct": None,
                "vrp": None,
                "entry_iv": None,
                "rv": None,
                "rv_window_days": 0,
                "greeks": {
                    "delta": 1.0, "gamma": 0, "theta": 0, "vega": 0,
                    "vanna": 0, "charm": 0, "vomma": 0,
                    "implied_volatility": 0,
                },
            }],
        }
        out = _format_position_context(
            "AAPL", stock_summary, spot_price=155.0,
            beta=1.2, beta_weighted_delta=120.0,
        )
        # The stock summary line is present, but P&L/VRP markers are not.
        assert "STOCK: 100 shares" in out
        assert "P&L" not in out
        assert "VRP" not in out
