"""IB Flex Query parsing and fetching for position import."""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from positionoracle.types import ContractType, FlexReport, OpeningTrade, Position

if TYPE_CHECKING:
    from positionoracle.types import SymbolLoss

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ``openCloseIndicator`` values that denote a closing trade. IB sometimes
# emits combined values like "C;O" for partial closes.
_CLOSE_INDICATORS = {"C", "C;O"}
# Indicators that denote an opening trade (full open or a partial that
# opens new contracts alongside closing some).
_OPEN_INDICATORS = {"O", "O;C", "C;O"}
_ZERO = Decimal(0)


def _merge_open_position(
    seen: dict[str, Position],
    summary_locked: set[str],
    level: str,
    pos: Position,
) -> None:
    """Merge one parsed ``OpenPosition`` row into the accumulator.

    IB Flex Queries can be configured at ``SUMMARY`` and/or ``LOT``
    level of detail. A lot-level query emits one row per tax lot, so a
    single holding of 200 shares bought in two 100-share lots arrives
    as two rows for the same symbol. Those lots must be summed or the
    position size is understated — a plain ``seen[symbol] = ...``
    overwrite keeps only the last lot (the "200 collapses to 100" bug).

    When both a ``SUMMARY`` row and its constituent ``LOT`` rows are
    present for the same symbol, the summary already carries the
    aggregated total and is authoritative; its lot rows are ignored so
    the two are not double-counted. A query with no ``levelOfDetail``
    attribute at all yields one row per symbol, which sums to itself.

    Parameters
    ----------
    seen : dict[str, Position]
        Accumulator keyed by symbol, mutated in place.
    summary_locked : set[str]
        Symbols for which an authoritative ``SUMMARY`` row was seen.
    level : str
        Upper-cased ``levelOfDetail`` attribute of the row.
    pos : Position
        The position parsed from this single row.
    """
    symbol = pos.symbol
    if level == "SUMMARY":
        seen[symbol] = pos
        summary_locked.add(symbol)
        return
    if symbol in summary_locked:
        # A SUMMARY row already accounts for this symbol's total.
        return
    existing = seen.get(symbol)
    if existing is None:
        seen[symbol] = pos
        return
    seen[symbol] = replace(
        existing,
        quantity=existing.quantity + pos.quantity,
        cost_basis=existing.cost_basis + pos.cost_basis,
    )


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

    # Accumulate lot-level entries keyed by symbol. iter("OpenPosition")
    # recurses into nested elements, so lot-level Flex Queries can yield
    # both a summary row and its per-lot rows for the same symbol.
    # ``_merge_open_position`` sums lots (so 200 shares held as two
    # 100-share lots stays 200) while preferring an authoritative SUMMARY
    # row when one is present.
    seen: dict[str, Position] = {}
    summary_locked: set[str] = set()

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
                    _merge_open_position(
                        seen,
                        summary_locked,
                        pos_elem.get("levelOfDetail", "").upper(),
                        Position(
                            symbol=symbol,
                            underlying=symbol,
                            contract_type=ContractType.STOCK,
                            strike=0.0,
                            expiration=datetime.date.max,
                            quantity=quantity,
                            cost_basis=cost_basis,
                            multiplier=1,
                        ),
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

                _merge_open_position(
                    seen,
                    summary_locked,
                    pos_elem.get("levelOfDetail", "").upper(),
                    Position(
                        symbol=symbol,
                        underlying=underlying,
                        contract_type=contract_type,
                        strike=strike,
                        expiration=expiration,
                        quantity=quantity,
                        cost_basis=cost_basis,
                        multiplier=multiplier,
                    ),
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


def _parse_trade_datetime(value: str) -> datetime.datetime | None:
    """Parse IB's ``tradeDateTime`` (or ``dateTime``) into an aware ET datetime.

    IB usually formats this as ``YYYYMMDD;HHMMSS`` but some Flex
    configurations emit ISO 8601. Falls back to ``None`` if neither
    parses.
    """
    if not value:
        return None
    # IB compact form first.
    try:
        date_part, time_part = value.split(";", 1)
        dt = datetime.datetime.strptime(
            f"{date_part}{time_part}", "%Y%m%d%H%M%S",
        )
        return dt.replace(tzinfo=_ET)
    except ValueError:
        pass
    # Try ISO 8601 (with optional T separator).
    try:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ET)
        return dt
    except ValueError:
        logger.warning("Could not parse tradeDateTime=%r", value)
        return None


def extract_opening_trades(xml_content: str) -> dict[str, OpeningTrade]:
    """Extract earliest opening trade per option symbol from a Flex XML.

    Walks every ``<Trade>`` element with ``assetCategory="OPT"`` and an
    opening indicator. For symbols that appear in multiple opening
    trades (averaged-in positions) only the earliest is retained — the
    cost-basis-weighted entry price comes from the ``OpenPosition``
    record, so we just need the entry *time* for the spot lookup.

    Parameters
    ----------
    xml_content : str
        Raw Flex Query XML.

    Returns
    -------
    dict[str, OpeningTrade]
        ``{symbol: OpeningTrade}`` for each option symbol with at least
        one opening trade.
    """
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        logger.exception("Failed to parse Flex Query XML for opening trades")
        return {}

    earliest: dict[str, OpeningTrade] = {}
    for trade in root.iter("Trade"):
        if trade.get("assetCategory", "") != "OPT":
            continue
        indicator = trade.get("openCloseIndicator", "")
        if indicator not in _OPEN_INDICATORS:
            continue

        symbol = trade.get("symbol", "").strip()
        underlying = trade.get("underlyingSymbol", "").strip().upper()
        if not symbol or not underlying:
            continue

        dt_str = (
            trade.get("tradeDateTime")
            or trade.get("dateTime")
            or trade.get("orderTime")
            or ""
        )
        trade_dt = _parse_trade_datetime(dt_str)
        if trade_dt is None:
            # Fall back to tradeDate at market open if no time is given.
            date_str = trade.get("tradeDate", "")
            if date_str:
                try:
                    trade_date = datetime.datetime.strptime(
                        date_str, "%Y%m%d",
                    ).date()
                except ValueError:
                    try:
                        trade_date = datetime.date.fromisoformat(date_str)
                    except ValueError:
                        logger.warning(
                            "Trade %s missing parseable date — skipping",
                            trade.get("tradeID", "?"),
                        )
                        continue
                trade_dt = datetime.datetime.combine(
                    trade_date, datetime.time(9, 30), tzinfo=_ET,
                )
            else:
                continue

        try:
            trade_price = float(trade.get("tradePrice", "0"))
            quantity = int(float(trade.get("quantity", "0")))
        except (ValueError, TypeError):
            continue
        if trade_price <= 0:
            continue

        candidate = OpeningTrade(
            symbol=symbol,
            underlying=underlying,
            trade_datetime=trade_dt,
            trade_price=trade_price,
            quantity=quantity,
        )
        existing = earliest.get(symbol)
        if existing is None or candidate.trade_datetime < existing.trade_datetime:
            earliest[symbol] = candidate

    logger.info("Extracted %d opening trade(s) from Flex Query", len(earliest))
    return earliest


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
        opening_trades=extract_opening_trades(xml_content),
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
