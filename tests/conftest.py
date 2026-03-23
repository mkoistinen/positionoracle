
import pytest


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def sample_flex_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Test" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567">
      <OpenPositions>
        <OpenPosition
          assetCategory="OPT"
          symbol="AAPL  251219C00150000"
          underlyingSymbol="AAPL"
          putCall="C"
          strike="150"
          expiry="20251219"
          position="10"
          costBasisMoney="5000.00"
          multiplier="100"
        />
        <OpenPosition
          assetCategory="OPT"
          symbol="AAPL  251219P00140000"
          underlyingSymbol="AAPL"
          putCall="P"
          strike="140"
          expiry="20251219"
          position="-5"
          costBasisMoney="-2500.00"
          multiplier="100"
        />
        <OpenPosition
          assetCategory="STK"
          symbol="AAPL"
          underlyingSymbol="AAPL"
          position="100"
          costBasisMoney="14700.00"
          multiplier="1"
        />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""
