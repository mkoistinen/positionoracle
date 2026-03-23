from positionoracle.greeks import compute_greeks, compute_greeks_from_massive
from positionoracle.types import ContractType

CALL = ContractType.CALL
PUT = ContractType.PUT


class TestComputeGreeksFromMassive:
    def test_uses_provided_first_order_greeks(self):
        g = compute_greeks_from_massive(
            s=100, k=100, t=0.25, r=0.05,
            contract_type=CALL,
            delta=0.55, gamma=0.04, theta=-0.03, vega=0.15, iv=0.20,
        )
        assert g.delta == 0.55
        assert g.gamma == 0.04
        assert g.theta == -0.03
        assert g.vega == 0.15

    def test_computes_second_order_greeks(self):
        g = compute_greeks_from_massive(
            s=100, k=100, t=0.25, r=0.05,
            contract_type=CALL,
            delta=0.55, gamma=0.04, theta=-0.03, vega=0.15, iv=0.20,
        )
        assert g.vanna != 0
        assert g.charm != 0
        assert g.vomma != 0

    def test_preserves_iv(self):
        g = compute_greeks_from_massive(
            s=100, k=100, t=0.25, r=0.05,
            contract_type=CALL,
            delta=0.55, gamma=0.04, theta=-0.03, vega=0.15, iv=0.35,
        )
        assert g.implied_volatility == 0.35

    def test_expired_returns_first_order_only(self):
        g = compute_greeks_from_massive(
            s=100, k=100, t=0, r=0.05,
            contract_type=CALL,
            delta=1.0, gamma=0.0, theta=0.0, vega=0.0, iv=0.20,
        )
        assert g.delta == 1.0
        assert g.vanna == 0.0
        assert g.charm == 0.0
        assert g.vomma == 0.0

    def test_zero_iv_returns_first_order_only(self):
        g = compute_greeks_from_massive(
            s=100, k=100, t=0.25, r=0.05,
            contract_type=CALL,
            delta=0.55, gamma=0.04, theta=-0.03, vega=0.15, iv=0,
        )
        assert g.delta == 0.55
        assert g.vanna == 0.0

    def test_second_order_consistent_with_full_bs(self):
        bs = compute_greeks(
            s=100, k=100, t=0.25, r=0.05, sigma=0.20, contract_type=CALL,
        )
        hybrid = compute_greeks_from_massive(
            s=100, k=100, t=0.25, r=0.05,
            contract_type=CALL,
            delta=bs.delta, gamma=bs.gamma, theta=bs.theta,
            vega=bs.vega, iv=0.20,
        )
        assert abs(hybrid.vanna - bs.vanna) < 0.01
        assert abs(hybrid.charm - bs.charm) < 0.001

    def test_put_second_order(self):
        g = compute_greeks_from_massive(
            s=100, k=100, t=0.25, r=0.05,
            contract_type=PUT,
            delta=-0.45, gamma=0.04, theta=-0.02, vega=0.15, iv=0.20,
        )
        assert g.vanna != 0
        assert g.charm != 0
