import datetime

from positionoracle.flex import build_massive_ticker, parse_flex_xml
from positionoracle.types import ContractType, Position


class TestParseFlexXml:
    def test_parses_all_positions(self, sample_flex_xml):
        positions = parse_flex_xml(sample_flex_xml)
        assert len(positions) == 3

    def test_includes_stock_positions(self, sample_flex_xml):
        positions = parse_flex_xml(sample_flex_xml)
        stocks = [p for p in positions if p.contract_type == ContractType.STOCK]
        assert len(stocks) == 1
        assert stocks[0].underlying == "AAPL"
        assert stocks[0].quantity == 100
        assert stocks[0].multiplier == 1

    def test_call_parsed_correctly(self, sample_flex_xml):
        positions = parse_flex_xml(sample_flex_xml)
        calls = [p for p in positions if p.contract_type == ContractType.CALL]
        assert len(calls) == 1
        call = calls[0]
        assert call.underlying == "AAPL"
        assert call.strike == 150.0
        assert call.expiration == datetime.date(2025, 12, 19)
        assert call.quantity == 10
        assert call.multiplier == 100

    def test_put_parsed_correctly(self, sample_flex_xml):
        positions = parse_flex_xml(sample_flex_xml)
        puts = [p for p in positions if p.contract_type == ContractType.PUT]
        assert len(puts) == 1
        put = puts[0]
        assert put.strike == 140.0
        assert put.quantity == -5

    def test_empty_xml_returns_empty(self):
        positions = parse_flex_xml("<root></root>")
        assert positions == []

    def test_invalid_xml_returns_empty(self):
        positions = parse_flex_xml("not xml at all")
        assert positions == []


class TestBuildMassiveTicker:
    def test_call_ticker(self):
        pos = Position(
            symbol="AAPL  251219C00150000",
            underlying="AAPL",
            contract_type=ContractType.CALL,
            strike=150.0,
            expiration=datetime.date(2025, 12, 19),
            quantity=10,
            cost_basis=5000.0,
        )
        assert build_massive_ticker(pos) == "O:AAPL251219C00150000"

    def test_put_ticker(self):
        pos = Position(
            symbol="AAPL  251219P00140000",
            underlying="AAPL",
            contract_type=ContractType.PUT,
            strike=140.0,
            expiration=datetime.date(2025, 12, 19),
            quantity=-5,
            cost_basis=-2500.0,
        )
        assert build_massive_ticker(pos) == "O:AAPL251219P00140000"
