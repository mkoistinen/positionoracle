import datetime

from positionoracle.advisor import build_portfolio_summary, evaluate_position
from positionoracle.types import (
    AdviceLevel,
    ContractType,
    Greeks,
    Position,
    PositionGreeks,
)


def _make_pg(
    delta=0.5,
    gamma=0.05,
    theta=-0.03,
    vega=0.10,
    quantity=1,
    dte=30,
    contract_type=ContractType.CALL,
):
    pos = Position(
        symbol="TEST251219C00100000",
        underlying="TEST",
        contract_type=contract_type,
        strike=100.0,
        expiration=datetime.date.today() + datetime.timedelta(days=dte),
        quantity=quantity,
        cost_basis=500.0,
    )
    greeks = Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
    )
    return PositionGreeks(position=pos, greeks=greeks, underlying_price=100.0)


DEFAULT_THRESHOLDS = {
    "delta_warn": 0.30,
    "delta_urgent": 0.50,
    "gamma_warn": 0.10,
    "theta_warn": -0.05,
    "vega_warn": 0.20,
    "dte_gamma_warn": 7,
}


class TestEvaluatePosition:
    def test_no_advice_for_safe_position(self):
        pg = _make_pg(delta=0.20, gamma=0.02, theta=-0.01, vega=0.05)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        assert len(advice) == 0

    def test_delta_warning(self):
        pg = _make_pg(delta=0.40)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        deltas = [a for a in advice if a.metric == "delta"]
        assert len(deltas) == 1
        assert deltas[0].level == AdviceLevel.WARNING

    def test_delta_urgent(self):
        pg = _make_pg(delta=0.60)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        deltas = [a for a in advice if a.metric == "delta"]
        assert len(deltas) == 1
        assert deltas[0].level == AdviceLevel.URGENT

    def test_gamma_near_expiry(self):
        pg = _make_pg(gamma=0.15, dte=3)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        gammas = [a for a in advice if a.metric == "gamma"]
        assert len(gammas) == 1
        assert gammas[0].level == AdviceLevel.URGENT

    def test_gamma_not_urgent_far_from_expiry(self):
        pg = _make_pg(gamma=0.15, dte=30)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        gammas = [a for a in advice if a.metric == "gamma"]
        assert len(gammas) == 0

    def test_theta_decay_warning_for_long(self):
        pg = _make_pg(theta=-0.10, quantity=1)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        thetas = [a for a in advice if a.metric == "theta"]
        assert len(thetas) == 1

    def test_theta_no_warning_for_short(self):
        pg = _make_pg(theta=-0.10, quantity=-1)
        advice = evaluate_position(pg, DEFAULT_THRESHOLDS)
        thetas = [a for a in advice if a.metric == "theta"]
        assert len(thetas) == 0


class TestBuildPortfolioSummary:
    def test_aggregates_by_underlying(self):
        pgs = [
            _make_pg(delta=0.50, quantity=10),
            _make_pg(delta=-0.30, quantity=5),
        ]
        summaries = build_portfolio_summary(pgs, DEFAULT_THRESHOLDS)
        assert "TEST" in summaries
        summary = summaries["TEST"]
        expected_delta = 0.50 * 10 * 100 + (-0.30) * 5 * 100
        assert abs(summary.net_delta - expected_delta) < 0.01
