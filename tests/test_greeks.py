from positionoracle.greeks import compute_greeks
from positionoracle.types import ContractType

CALL = ContractType.CALL
PUT = ContractType.PUT

# Common params: ATM, 3-month, 5% rate, 20% vol
_ATM = {"s": 100, "k": 100, "t": 0.25, "r": 0.05, "sigma": 0.20}


class TestComputeGreeks:
    def test_atm_call_delta_near_half(self):
        g = compute_greeks(**_ATM, contract_type=CALL)
        assert 0.45 < g.delta < 0.65

    def test_atm_put_delta_near_negative_half(self):
        g = compute_greeks(**_ATM, contract_type=PUT)
        assert -0.55 < g.delta < -0.35

    def test_deep_itm_call_delta_near_one(self):
        g = compute_greeks(
            s=200, k=100, t=0.25, r=0.05, sigma=0.20, contract_type=CALL,
        )
        assert g.delta > 0.99

    def test_deep_otm_call_delta_near_zero(self):
        g = compute_greeks(
            s=50, k=100, t=0.25, r=0.05, sigma=0.20, contract_type=CALL,
        )
        assert g.delta < 0.01

    def test_gamma_positive(self):
        g = compute_greeks(**_ATM, contract_type=CALL)
        assert g.gamma > 0

    def test_gamma_same_for_call_and_put(self):
        call = compute_greeks(**_ATM, contract_type=CALL)
        put = compute_greeks(**_ATM, contract_type=PUT)
        assert abs(call.gamma - put.gamma) < 1e-10

    def test_theta_negative_for_long(self):
        g = compute_greeks(**_ATM, contract_type=CALL)
        assert g.theta < 0

    def test_vega_positive(self):
        g = compute_greeks(**_ATM, contract_type=CALL)
        assert g.vega > 0

    def test_vega_same_for_call_and_put(self):
        call = compute_greeks(**_ATM, contract_type=CALL)
        put = compute_greeks(**_ATM, contract_type=PUT)
        assert abs(call.vega - put.vega) < 1e-10

    def test_second_order_greeks_computed(self):
        g = compute_greeks(**_ATM, contract_type=CALL)
        assert g.vanna != 0
        assert g.charm != 0
        assert g.vomma != 0

    def test_expired_option_returns_zeros(self):
        g = compute_greeks(
            s=100, k=100, t=0, r=0.05, sigma=0.20, contract_type=CALL,
        )
        assert g.delta == 0.0
        assert g.gamma == 0.0

    def test_iv_preserved(self):
        g = compute_greeks(
            s=100, k=100, t=0.25, r=0.05, sigma=0.35, contract_type=CALL,
        )
        assert g.implied_volatility == 0.35
