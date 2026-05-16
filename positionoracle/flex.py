"""IB Flex Query parsing and fetching for position import."""

from __future__ import annotations

import asyncio
import datetime
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from positionoracle.types import ContractType, FlexReport, Position

if TYPE_CHECKING:
    from positionoracle.types import SymbolLoss

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ``openCloseIndicator`` values that denote a closing trade. IB sometimes
# emits combined values like "C;O" for partial closes.
_CLOSE_INDICATORS = {"C", "C;O"}
_ZERO = Decimal(0)


def parse_flex_xml(xml_content: str) -> list[Position]:
    """Parse an IB Flex Query XML response into Position objects.

    Handles the ``OpenPositions`` section of a Flex Query report.

    Parameters
    ----------
    xml_content : str
        Raw XML content from the Flex Query.

    Returns
    -------
    list[Position]
        Parsed option positions. Non-option positions are skipped.
    """
    try:
        from xml.etree import ElementTree as ET
    except ImportError:
        logger.exception("xml.etree not available")
        return []

    # Use a dict keyed by symbol to deduplicate lot-level entries.
    # iter("OpenPosition") recurses into nested elements, so lot-level
    # Flex Queries can yield both summary and lot rows for the same symbol.
    seen: dict[str, Position] = {}

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        logger.exception("Failed to parse Flex Query XML")
        return []

    for stmt in root.iter("FlexStatement"):
        for pos_elem in stmt.iter("OpenPosition"):
            asset_category = pos_elem.get("assetCategory", "")

            if asset_category == "STK":
                try:
                    symbol = pos_elem.get("symbol", "")
                    quantity = int(float(pos_elem.get("position", "0")))
                    cost_basis = float(
                        pos_elem.get("costBasisMoney", pos_elem.get("costBasis", "0"))
                    )
                    if not symbol or quantity == 0:
                        continue
                    seen[symbol] = Position(
                        symbol=symbol,
                        underlying=symbol,
                        contract_type=ContractType.STOCK,
                        strike=0.0,
                        expiration=datetime.date.max,
                        quantity=quantity,
                        cost_basis=cost_basis,
                        multiplier=1,
                    )
                except (ValueError, TypeError):
                    logger.exception("Failed to parse stock position: %s", symbol)
                continue

            if asset_category != "OPT":
                continue

            symbol = pos_elem.get("symbol", "")
            underlying = pos_elem.get("underlyingSymbol", "")
            put_call = pos_elem.get("putCall", "").upper()
            strike_str = pos_elem.get("strike", "0")
            expiry_str = pos_elem.get("expiry", "")
            quantity_str = pos_elem.get("position", "0")
            cost_basis_str = pos_elem.get("costBasisMoney", pos_elem.get("costBasis", "0"))
            multiplier_str = pos_elem.get("multiplier", "100")

            if not underlying or not expiry_str:
                continue

            try:
                contract_type = ContractType.CALL if put_call == "C" else ContractType.PUT
                strike = float(strike_str)
                quantity = int(float(quantity_str))
                cost_basis = float(cost_basis_str)
                multiplier = int(float(multiplier_str))

                if quantity == 0:
                    continue

                # IB uses YYYYMMDD format for expiry
                if len(expiry_str) == 8:
                    expiration = datetime.date(
                        int(expiry_str[:4]),
                        int(expiry_str[4:6]),
                        int(expiry_str[6:8]),
                    )
                else:
                    expiration = datetime.date.fromisoformat(expiry_str)

                seen[symbol] = Position(
                    symbol=symbol,
                    underlying=underlying,
                    contract_type=contract_type,
                    strike=strike,
                    expiration=expiration,
                    quantity=quantity,
                    cost_basis=cost_basis,
                    multiplier=multiplier,
                )
            except (ValueError, TypeError):
                logger.exception("Failed to parse position: %s", symbol)

    positions = list(seen.values())
    logger.info("Parsed %d positions from Flex Query", len(positions))
    return positions


def _parse_when_generated(value: str) -> datetime.datetime | None:
    """Parse IB's ``whenGenerated`` attribute into a tz-aware ET datetime.

    IB formats the value as ``YYYYMMDD;HHMMSS`` in Eastern Time. Returns
    ``None`` if the value is missing or malformed; the caller decides
    how to fall back.

    Parameters
    ----------
    value : str
        Raw attribute value from the ``<FlexStatement>`` element.

    Returns
    -------
    datetime.datetime | None
        Aware datetime in ``America/New_York``, or None on failure.
    """
    if not value:
        return None
    try:
        date_part, time_part = value.split(";", 1)
        dt = datetime.datetime.strptime(
            f"{date_part}{time_part}", "%Y%m%d%H%M%S",
        )
    except ValueError:
        logger.warning("Could not parse whenGenerated=%r", value)
        return None
    return dt.replace(tzinfo=_ET)


def extract_losses(xml_content: str) -> list[SymbolLoss]:
    """Extract realized-loss closing trades from a Flex Query XML.

    Walks every ``<Trade>`` element and emits a ``(symbol, date)`` pair
    for each one that is (a) a closing trade and (b) has negative
    ``fifoPnlRealized``. Underlying symbol is resolved from
    ``underlyingSymbol`` when present, falling back to the OCC option
    symbol's leading token for options or the bare ``symbol`` for stock.

    Parameters
    ----------
    xml_content : str
        Raw Flex Query XML.

    Returns
    -------
    list[SymbolLoss]
        ``(underlying_symbol, trade_date)`` pairs.
    """
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        logger.exception("Failed to parse Flex Query XML for losses")
        return []

    losses: list[SymbolLoss] = []
    for trade in root.iter("Trade"):
        if trade.get("openCloseIndicator", "") not in _CLOSE_INDICATORS:
            continue

        pnl_str = trade.get("fifoPnlRealized", "")
        if not pnl_str:
            continue
        try:
            pnl = Decimal(pnl_str)
        except InvalidOperation:
            continue
        if pnl >= _ZERO:
            continue

        underlying = trade.get("underlyingSymbol", "").strip().upper()
        if not underlying:
            cat = trade.get("assetCategory", "")
            sym = trade.get("symbol", "").strip().upper()
            if cat == "STK":
                underlying = sym
            elif cat == "OPT" and sym:
                underlying = sym.split()[0]
        if not underlying:
            logger.warning(
                "Could not resolve underlying for trade %s",
                trade.get("tradeID", "?"),
            )
            continue

        date_str = trade.get("tradeDate", "")
        if not date_str:
            logger.warning(
                "Trade %s missing tradeDate — skipping",
                trade.get("tradeID", "?"),
            )
            continue
        try:
            trade_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            try:
                trade_date = datetime.datetime.strptime(
                    date_str, "%Y%m%d",
                ).date()
            except ValueError:
                logger.warning("Unparseable tradeDate %r — skipping", date_str)
                continue

        losses.append((underlying, trade_date))

    logger.info("Extracted %d realized-loss trade(s) from Flex Query", len(losses))
    return losses


def parse_flex_report(xml_content: str) -> FlexReport:
    """Parse a Flex Query XML response into a FlexReport.

    Reads ``whenGenerated`` from the ``<FlexStatement>`` element and
    pairs it with the parsed positions. If ``whenGenerated`` is missing
    or malformed, falls back to the current ET time and logs a warning.

    Parameters
    ----------
    xml_content : str
        Raw XML content from the Flex Query.

    Returns
    -------
    FlexReport
        Aware ET timestamp paired with parsed positions.
    """
    from xml.etree import ElementTree as ET

    when_generated: datetime.datetime | None = None
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        logger.exception("Failed to parse Flex Query XML for whenGenerated")
        root = None

    if root is not None:
        for stmt in root.iter("FlexStatement"):
            when_generated = _parse_when_generated(stmt.get("whenGenerated", ""))
            if when_generated is not None:
                break

    if when_generated is None:
        logger.warning(
            "Flex report missing whenGenerated; falling back to current ET time",
        )
        when_generated = datetime.datetime.now(tz=_ET)

    return FlexReport(
        when_generated=when_generated,
        positions=parse_flex_xml(xml_content),
        losses=extract_losses(xml_content),
    )


def _download(token: str, query_id: str) -> bytes:
    """Download a Flex Query report via the ibflex HTTP client (blocking I/O).

    Parameters
    ----------
    token : str
        IB Flex Web Service API token.
    query_id : str
        Flex Query ID.

    Returns
    -------
    bytes
        Raw XML response.
    """
    from ibflex import client

    return client.download(token, query_id)


async def fetch_positions(token: str, query_id: str) -> FlexReport:
    """Download a Flex Query report from IB and parse it.

    Runs the blocking ibflex download in a thread pool.

    Parameters
    ----------
    token : str
        IB Flex Web Service API token.
    query_id : str
        Flex Query ID.

    Returns
    -------
    FlexReport
        Parsed report with the IB-stamped ``whenGenerated`` timestamp
        and the parsed positions.

    Raises
    ------
    Exception
        If the download or parsing fails.
    """
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(None, _download, token, query_id)

    xml_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return parse_flex_report(xml_str)


def build_massive_ticker(position: Position) -> str:
    """Build a Massive-style options ticker from a Position.

    Format: ``O:AAPL251219C00150000``

    Parameters
    ----------
    position : Position
        The option position.

    Returns
    -------
    str
        Massive-format option ticker.
    """
    put_call = "C" if position.contract_type == ContractType.CALL else "P"
    expiry = position.expiration.strftime("%y%m%d")
    # Strike is in dollars, padded to 8 digits with 3 implied decimals
    strike_int = int(position.strike * 1000)
    strike_str = f"{strike_int:08d}"
    return f"O:{position.underlying}{expiry}{put_call}{strike_str}"
