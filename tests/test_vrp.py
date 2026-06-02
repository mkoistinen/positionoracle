"""Tests for VRP math helpers."""

from __future__ import annotations

import math

import pytest

from positionoracle.types import ContractType
from positionoracle.vrp import (
    bs_price,
    implied_vol,
    realized_vol_annualized,
    vrp_ratio,
)


class TestRealizedVolAnnualized:
    def test_constant_series_returns_zero(self):
        closes = [100.0] * 30
        assert realized_vol_annualized(closes, window=21) == pytest.approx(0.0)

    def test_too_few_closes_returns_nan(self):
        assert math.isnan(realized_vol_annualized([100.0]))

    def test_annualization_matches_formula(self):
        # Two daily returns of +1% and -1% (alternating).
        closes = [100.0]
        for i in range(20):
            mult = 1.01 if i % 2 == 0 else (1 / 1.01)
            closes.append(closes[-1] * mult)
        rv = realized_vol_annualized(closes, window=21)
        # Each return is roughly ±ln(1.01) ≈ ±0.00995. With 20 returns,
        # sum_sq ≈ 20 * 0.00995^2; sqrt(252/20 * sum_sq) ≈ 0.1576.
        assert 0.15 < rv < 0.17

    def test_uses_trailing_window(self):
        # Calm prefix + volatile tail — windowed result should reflect tail only.
        calm = [100.0] * 100
        volatile = [100.0]
        for _ in range(22):
            volatile.append(volatile[-1] * (1.05 if len(volatile) % 2 else 0.95))
        rv_all = realized_vol_annualized([*calm, *volatile], window=21)
        rv_tail = realized_vol_annualized(volatile, window=21)
        assert rv_all == pytest.approx(rv_tail, rel=1e-9)

    def test_skips_non_positive_closes(self):
        # A zero close in the middle is silently skipped.
        closes = [100.0, 101.0, 0.0, 102.0, 103.0]
        rv = realized_vol_annualized(closes, window=21)
        assert rv > 0 and not math.isnan(rv)


class TestBSPrice:
    def test_call_intrinsic_when_no_time(self):
        # T=0 short-circuits to 0.0 by our convention.
        assert bs_price(110, 100, 0, 0.05, 0.2, ContractType.CALL) == 0.0

    def test_call_price_increases_with_vol(self):
        lo = bs_price(100, 100, 0.25, 0.05, 0.1, ContractType.CALL)
        hi = bs_price(100, 100, 0.25, 0.05, 0.5, ContractType.CALL)
        assert hi > lo > 0


class TestImpliedVol:
    @pytest.mark.parametrize("sigma_truth", [0.10, 0.20, 0.35, 0.80])
    def test_round_trip_call(self, sigma_truth):
        s, k, t, r = 100.0, 100.0, 0.25, 0.04
        price = bs_price(s, k, t, r, sigma_truth, ContractType.CALL)
        recovered = implied_vol(price, s, k, t, r, ContractType.CALL)
        assert recovered == pytest.approx(sigma_truth, abs=1e-4)

    @pytest.mark.parametrize("sigma_truth", [0.15, 0.30, 0.60])
    def test_round_trip_put(self, sigma_truth):
        s, k, t, r = 95.0, 100.0, 0.5, 0.03
        price = bs_price(s, k, t, r, sigma_truth, ContractType.PUT)
        recovered = implied_vol(price, s, k, t, r, ContractType.PUT)
        assert recovered == pytest.approx(sigma_truth, abs=1e-4)

    def test_price_below_intrinsic_returns_nan(self):
        # A call worth less than intrinsic value (S-K disc) is impossible.
        s, k, t, r = 150.0, 100.0, 0.5, 0.05
        nonsense_price = 1.0
        assert math.isnan(
            implied_vol(nonsense_price, s, k, t, r, ContractType.CALL),
        )

    def test_zero_price_returns_nan(self):
        assert math.isnan(implied_vol(0.0, 100, 100, 0.25, 0.05, ContractType.CALL))


class TestVRPRatio:
    def test_basic(self):
        assert vrp_ratio(0.20, 0.40) == pytest.approx(0.5)

    def test_returns_nan_on_zero(self):
        assert math.isnan(vrp_ratio(0.0, 0.4))
        assert math.isnan(vrp_ratio(0.2, 0.0))

    def test_returns_nan_on_nan(self):
        assert math.isnan(vrp_ratio(float("nan"), 0.3))
