"""IB Flex Query parsing and fetching for position import."""

from __future__ import annotations

import asyncio
import datetime
import logging

from positionoracle.types import ContractType, Position

logger = logging.getLogger(__name__)


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


async def fetch_positions(token: str, query_id: str) -> list[Position]:
    """Download a Flex Query report from IB and parse positions.

    Runs the blocking ibflex download in a thread pool.

    Parameters
    ----------
    token : str
        IB Flex Web Service API token.
    query_id : str
        Flex Query ID.

    Returns
    -------
    list[Position]
        Parsed option positions from the Flex Query.

    Raises
    ------
    Exception
        If the download or parsing fails.
    """
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(None, _download, token, query_id)

    xml_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return parse_flex_xml(xml_str)


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
