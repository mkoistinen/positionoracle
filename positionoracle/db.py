"""SQLite database for position persistence."""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

import aiosqlite

from positionoracle.types import ContractType, Position

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    underlying TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    strike REAL NOT NULL,
    expiration TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    cost_basis REAL NOT NULL,
    multiplier INTEGER NOT NULL DEFAULT 100,
    imported_at TEXT NOT NULL
)
"""

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def db_path(data_dir: Path) -> str:
    """Return the SQLite database file path.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    str
        Absolute path to the SQLite database file.
    """
    return str(data_dir / "positionoracle.db")


async def init_db(data_dir: Path) -> None:
    """Create database tables if they don't exist.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        await conn.execute(_CREATE_POSITIONS)
        await conn.execute(_CREATE_SETTINGS)
        await conn.commit()
    logger.info("Database initialized at %s", db_path(data_dir))


async def upsert_positions(data_dir: Path, positions: list[Position]) -> int:
    """Insert or replace positions in the database.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    positions : list[Position]
        Positions to upsert (keyed by symbol).

    Returns
    -------
    int
        Number of positions upserted.
    """
    now = datetime.datetime.now(tz=datetime.UTC).isoformat()
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        for pos in positions:
            await conn.execute(
                """
                INSERT INTO positions
                    (symbol, underlying, contract_type, strike, expiration,
                     quantity, cost_basis, multiplier, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    underlying = excluded.underlying,
                    contract_type = excluded.contract_type,
                    strike = excluded.strike,
                    expiration = excluded.expiration,
                    quantity = excluded.quantity,
                    cost_basis = excluded.cost_basis,
                    multiplier = excluded.multiplier,
                    imported_at = excluded.imported_at
                """,
                (
                    pos.symbol,
                    pos.underlying,
                    pos.contract_type.value,
                    pos.strike,
                    pos.expiration.isoformat(),
                    pos.quantity,
                    pos.cost_basis,
                    pos.multiplier,
                    now,
                ),
            )
        await conn.commit()
    return len(positions)


async def load_positions(data_dir: Path) -> list[Position]:
    """Load all positions from the database.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    list[Position]
        All stored positions.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM positions ORDER BY underlying, expiration, strike"
        )
        rows = await cursor.fetchall()

    return [
        Position(
            symbol=row["symbol"],
            underlying=row["underlying"],
            contract_type=ContractType(row["contract_type"]),
            strike=row["strike"],
            expiration=datetime.date.fromisoformat(row["expiration"]),
            quantity=row["quantity"],
            cost_basis=row["cost_basis"],
            multiplier=row["multiplier"],
        )
        for row in rows
    ]


async def delete_position(data_dir: Path, symbol: str) -> bool:
    """Delete a position by symbol.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    symbol : str
        Option symbol to delete.

    Returns
    -------
    bool
        True if a row was deleted.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        cursor = await conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        await conn.commit()
        return cursor.rowcount > 0


async def clear_positions(data_dir: Path) -> int:
    """Delete all positions.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    int
        Number of rows deleted.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        cursor = await conn.execute("DELETE FROM positions")
        await conn.commit()
        return cursor.rowcount


async def get_setting(data_dir: Path, key: str) -> str | None:
    """Retrieve a setting value.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    key : str
        Setting key.

    Returns
    -------
    str | None
        The setting value, or None if not found.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        cursor = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_setting(data_dir: Path, key: str, value: str) -> None:
    """Store a setting value.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    key : str
        Setting key.
    value : str
        Setting value.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await conn.commit()


async def get_thresholds(data_dir: Path) -> dict[str, float]:
    """Load advisor thresholds from the settings table.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    dict[str, float]
        Threshold settings with defaults applied.
    """
    defaults = {
        "delta_warn": 0.30,
        "delta_urgent": 0.50,
        "gamma_warn": 0.10,
        "theta_warn": -0.05,
        "vega_warn": 0.20,
        "dte_gamma_warn": 7,
    }
    raw = await get_setting(data_dir, "thresholds")
    if raw:
        stored = json.loads(raw)
        defaults.update(stored)
    return defaults
