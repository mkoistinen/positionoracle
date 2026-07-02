import datetime
from zoneinfo import ZoneInfo

from positionoracle.flex import (
    build_massive_ticker,
    extract_losses,
    parse_flex_report,
    parse_flex_xml,
)
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
        assert call.expiration == datetime.date(2099, 12, 19)
        assert call.quantity == 10
        assert call.multiplier == 100

    def test_put_parsed_correctly(self, sample_flex_xml):
        positions = parse_flex_xml(sample_flex_xml)
        puts = [p for p in positions if p.contract_type == ContractType.PUT]
        assert len(puts) == 1
        put = puts[0]
        assert put.strike == 140.0
        assert put.quantity == -5

    def test_lot_level_stock_rows_are_summed(self):
        """A lot-level query emits one row per tax lot; they must sum.

        Regression: buying another 100 shares of a symbol already held
        arrives as two 100-share lot rows. The old parser overwrote by
        symbol and kept only the last lot (200 collapsed to 100).
        """
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U1">
      <OpenPositions>
        <OpenPosition assetCategory="STK" symbol="AAPL"
          underlyingSymbol="AAPL" position="100" costBasisMoney="14700"
          multiplier="1" levelOfDetail="LOT"/>
        <OpenPosition assetCategory="STK" symbol="AAPL"
          underlyingSymbol="AAPL" position="100" costBasisMoney="15100"
          multiplier="1" levelOfDetail="LOT"/>
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        positions = parse_flex_xml(xml)
        stocks = [p for p in positions if p.contract_type == ContractType.STOCK]
        assert len(stocks) == 1
        assert stocks[0].quantity == 200
        assert stocks[0].cost_basis == 29800.0

    def test_summary_row_wins_over_its_lots(self):
        """A SUMMARY row already aggregates its lots; don't double-count."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U1">
      <OpenPositions>
        <OpenPosition assetCategory="STK" symbol="AAPL"
          underlyingSymbol="AAPL" position="200" costBasisMoney="29800"
          multiplier="1" levelOfDetail="SUMMARY"/>
        <OpenPosition assetCategory="STK" symbol="AAPL"
          underlyingSymbol="AAPL" position="100" costBasisMoney="14700"
          multiplier="1" levelOfDetail="LOT"/>
        <OpenPosition assetCategory="STK" symbol="AAPL"
          underlyingSymbol="AAPL" position="100" costBasisMoney="15100"
          multiplier="1" levelOfDetail="LOT"/>
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        positions = parse_flex_xml(xml)
        stocks = [p for p in positions if p.contract_type == ContractType.STOCK]
        assert len(stocks) == 1
        assert stocks[0].quantity == 200
        assert stocks[0].cost_basis == 29800.0

    def test_lot_level_option_rows_are_summed(self):
        """Option lots for the same OCC symbol sum, just like stock."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U1">
      <OpenPositions>
        <OpenPosition assetCategory="OPT" symbol="AAPL  991219C00150000"
          underlyingSymbol="AAPL" putCall="C" strike="150" expiry="20991219"
          position="5" costBasisMoney="2500" multiplier="100"
          levelOfDetail="LOT"/>
        <OpenPosition assetCategory="OPT" symbol="AAPL  991219C00150000"
          underlyingSymbol="AAPL" putCall="C" strike="150" expiry="20991219"
          position="5" costBasisMoney="2600" multiplier="100"
          levelOfDetail="LOT"/>
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        positions = parse_flex_xml(xml)
        calls = [p for p in positions if p.contract_type == ContractType.CALL]
        assert len(calls) == 1
        assert calls[0].quantity == 10
        assert calls[0].cost_basis == 5100.0

    def test_empty_xml_returns_empty(self):
        positions = parse_flex_xml("<root></root>")
        assert positions == []

    def test_invalid_xml_returns_empty(self):
        positions = parse_flex_xml("not xml at all")
        assert positions == []


class TestParseFlexReport:
    def test_extracts_when_generated(self, sample_flex_xml):
        report = parse_flex_report(sample_flex_xml)
        et = ZoneInfo("America/New_York")
        assert report.when_generated == datetime.datetime(
            2099, 12, 15, 17, 30, 45, tzinfo=et,
        )
        assert len(report.positions) == 3

    def test_missing_when_generated_falls_back_to_now(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement accountId="U1234567">
      <OpenPositions/>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        before = datetime.datetime.now(tz=ZoneInfo("America/New_York"))
        report = parse_flex_report(xml)
        after = datetime.datetime.now(tz=ZoneInfo("America/New_York"))
        assert before <= report.when_generated <= after
        assert report.when_generated.tzinfo is not None
        assert report.positions == []

    def test_malformed_when_generated_falls_back(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement whenGenerated="not-a-timestamp">
      <OpenPositions/>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
        report = parse_flex_report(xml)
        # Fallback uses current time; just confirm it's tz-aware.
        assert report.when_generated.tzinfo is not None
        assert report.positions == []

    def test_invalid_xml_falls_back(self):
        report = parse_flex_report("not xml at all")
        assert report.when_generated.tzinfo is not None
        assert report.positions == []


class TestExtractLosses:
    def _xml(self, *trades: str) -> str:
        joined = "\n".join(trades)
        return f"""<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements>
    <FlexStatement>
      <Trades>
{joined}
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

    def test_extracts_closing_loss_on_stock(self):
        xml = self._xml(
            '<Trade assetCategory="STK" symbol="AAPL" underlyingSymbol="AAPL" '
            'openCloseIndicator="C" tradeDate="2026-04-01" fifoPnlRealized="-123.45"/>',
        )
        losses = extract_losses(xml)
        assert losses == [("AAPL", datetime.date(2026, 4, 1))]

    def test_skips_opening_trade(self):
        xml = self._xml(
            '<Trade assetCategory="STK" symbol="AAPL" underlyingSymbol="AAPL" '
            'openCloseIndicator="O" tradeDate="2026-04-01" fifoPnlRealized="-50"/>',
        )
        assert extract_losses(xml) == []

    def test_skips_profitable_close(self):
        xml = self._xml(
            '<Trade assetCategory="STK" symbol="AAPL" underlyingSymbol="AAPL" '
            'openCloseIndicator="C" tradeDate="2026-04-01" fifoPnlRealized="100"/>',
        )
        assert extract_losses(xml) == []

    def test_accepts_partial_close_indicator(self):
        # IB emits "C;O" for trades that both close and re-open.
        xml = self._xml(
            '<Trade assetCategory="STK" symbol="MSFT" underlyingSymbol="MSFT" '
            'openCloseIndicator="C;O" tradeDate="2026-04-02" fifoPnlRealized="-1.50"/>',
        )
        assert extract_losses(xml) == [("MSFT", datetime.date(2026, 4, 2))]

    def test_option_resolves_underlying(self):
        # No underlyingSymbol attribute — must derive from OCC option symbol.
        xml = self._xml(
            '<Trade assetCategory="OPT" symbol="ALAB  260227P00150000" '
            'openCloseIndicator="C" tradeDate="2026-02-27" fifoPnlRealized="-300"/>',
        )
        assert extract_losses(xml) == [("ALAB", datetime.date(2026, 2, 27))]

    def test_yyyymmdd_trade_date(self):
        xml = self._xml(
            '<Trade assetCategory="STK" symbol="NVDA" underlyingSymbol="NVDA" '
            'openCloseIndicator="C" tradeDate="20260315" fifoPnlRealized="-42"/>',
        )
        assert extract_losses(xml) == [("NVDA", datetime.date(2026, 3, 15))]

    def test_invalid_pnl_skipped(self):
        xml = self._xml(
            '<Trade assetCategory="STK" symbol="AAPL" underlyingSymbol="AAPL" '
            'openCloseIndicator="C" tradeDate="2026-04-01" fifoPnlRealized=""/>',
            '<Trade assetCategory="STK" symbol="MSFT" underlyingSymbol="MSFT" '
            'openCloseIndicator="C" tradeDate="2026-04-01" fifoPnlRealized="not-a-number"/>',
        )
        assert extract_losses(xml) == []

    def test_malformed_xml_returns_empty(self):
        assert extract_losses("not xml") == []


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
