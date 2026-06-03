"""SQLite database for position persistence."""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

import aiosqlite

from positionoracle.types import (
    ApiKey,
    BlacklistEntry,
    ContractType,
    Position,
    PositionEntry,
)

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

_CREATE_BLACKLIST = """
CREATE TABLE IF NOT EXISTS blacklist (
    symbol TEXT PRIMARY KEY,
    loss_date TEXT NOT NULL,
    expires TEXT NOT NULL
)
"""

_CREATE_API_KEYS = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT
)
"""

_CREATE_POSITION_ENTRY = """
CREATE TABLE IF NOT EXISTS position_entry (
    symbol TEXT PRIMARY KEY,
    underlying TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    entry_spot REAL NOT NULL,
    entry_premium_per_share REAL NOT NULL,
    entry_iv REAL,
    entry_rate REAL NOT NULL,
    computed_at TEXT NOT NULL
)
"""

_WASH_SALE_WINDOW_DAYS = 30


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
        await conn.execute(_CREATE_BLACKLIST)
        await conn.execute(_CREATE_POSITION_ENTRY)
        await conn.execute(_CREATE_API_KEYS)
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
        # Remove positions no longer present in the incoming set.
        incoming_symbols = {pos.symbol for pos in positions}
        placeholders = ",".join("?" for _ in incoming_symbols)
        cursor = await conn.execute(
            f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})",
            tuple(incoming_symbols),
        )
        if cursor.rowcount:
            logger.info(
                "Removed %d stale position(s) not in incoming set of %d",
                cursor.rowcount,
                len(incoming_symbols),
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


async def delete_expired_positions(data_dir: Path, today_et: datetime.date) -> int:
    """Delete option positions with an expiration earlier than ``today_et``.

    Stock positions store ``expiration = datetime.date.max`` so they're
    never affected by this cleanup.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    today_et : datetime.date
        Current date in market timezone (America/New_York). Rows with
        ``expiration < today_et`` are deleted.

    Returns
    -------
    int
        Number of rows deleted.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        cursor = await conn.execute(
            "DELETE FROM positions WHERE expiration < ?",
            (today_et.isoformat(),),
        )
        await conn.commit()
        return cursor.rowcount


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


async def bulk_upsert_blacklist(
    data_dir: Path,
    losses: list[tuple[str, datetime.date]],
) -> int:
    """Upsert wash-sale blacklist entries.

    For each ``(symbol, loss_date)``: if no row exists for the symbol it
    is inserted; if a row exists and the new ``loss_date`` is more recent
    than the stored one, the row is updated; otherwise the existing row
    is left alone. Expiry is always ``loss_date + 30 days``. Symbols are
    upper-cased and stripped.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    losses : list[tuple[str, datetime.date]]
        Realized-loss closing trades.

    Returns
    -------
    int
        Number of rows that were inserted or updated. Note that SQLite
        reports the same per-row regardless of whether the WHERE clause
        elided the UPDATE — treat this as a best-effort count.
    """
    if not losses:
        return 0
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        for symbol, loss_date in losses:
            sym = symbol.upper().strip()
            if not sym:
                continue
            expires = loss_date + datetime.timedelta(days=_WASH_SALE_WINDOW_DAYS)
            await conn.execute(
                """
                INSERT INTO blacklist (symbol, loss_date, expires)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    loss_date = excluded.loss_date,
                    expires   = excluded.expires
                WHERE excluded.loss_date > blacklist.loss_date
                """,
                (sym, loss_date.isoformat(), expires.isoformat()),
            )
        await conn.commit()
    return len(losses)


async def prune_blacklist(data_dir: Path, today_et: datetime.date) -> int:
    """Delete blacklist rows whose expiry is before ``today_et``.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    today_et : datetime.date
        Current date in market timezone.

    Returns
    -------
    int
        Number of rows deleted.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        cursor = await conn.execute(
            "DELETE FROM blacklist WHERE expires < ?",
            (today_et.isoformat(),),
        )
        await conn.commit()
        return cursor.rowcount


async def load_blacklist(data_dir: Path) -> list[BlacklistEntry]:
    """Return active blacklist entries sorted by expiry ascending.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    list[BlacklistEntry]
        All currently-stored entries (caller should prune first).
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT symbol, loss_date, expires FROM blacklist "
            "ORDER BY expires ASC, symbol ASC"
        )
        rows = await cursor.fetchall()
    return [
        BlacklistEntry(
            symbol=row["symbol"],
            loss_date=datetime.date.fromisoformat(row["loss_date"]),
            expires=datetime.date.fromisoformat(row["expires"]),
        )
        for row in rows
    ]


async def upsert_position_entry(data_dir: Path, entry: PositionEntry) -> None:
    """Insert or replace a cached entry-data record for one position.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    entry : PositionEntry
        Entry-time data to persist.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        await conn.execute(
            """
            INSERT INTO position_entry
                (symbol, underlying, entry_time, entry_spot,
                 entry_premium_per_share, entry_iv, entry_rate, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                underlying = excluded.underlying,
                entry_time = excluded.entry_time,
                entry_spot = excluded.entry_spot,
                entry_premium_per_share = excluded.entry_premium_per_share,
                entry_iv = excluded.entry_iv,
                entry_rate = excluded.entry_rate,
                computed_at = excluded.computed_at
            """,
            (
                entry.symbol,
                entry.underlying,
                entry.entry_time.isoformat(),
                entry.entry_spot,
                entry.entry_premium_per_share,
                entry.entry_iv,
                entry.entry_rate,
                entry.computed_at.isoformat(),
            ),
        )
        await conn.commit()


async def load_position_entries(data_dir: Path) -> dict[str, PositionEntry]:
    """Return all cached ``PositionEntry`` rows keyed by symbol.

    Parameters
    ----------
    data_dir : Path
        Application data directory.

    Returns
    -------
    dict[str, PositionEntry]
        Cached entry data, keyed by option symbol.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM position_entry")
        rows = await cursor.fetchall()

    out: dict[str, PositionEntry] = {}
    for row in rows:
        out[row["symbol"]] = PositionEntry(
            symbol=row["symbol"],
            underlying=row["underlying"],
            entry_time=datetime.datetime.fromisoformat(row["entry_time"]),
            entry_spot=row["entry_spot"],
            entry_premium_per_share=row["entry_premium_per_share"],
            entry_iv=row["entry_iv"],
            entry_rate=row["entry_rate"],
            computed_at=datetime.datetime.fromisoformat(row["computed_at"]),
        )
    return out


async def delete_position_entries_not_in(
    data_dir: Path,
    keep_symbols: set[str],
) -> int:
    """Remove cached entry rows whose symbol isn't in ``keep_symbols``.

    Called after each position-import sync so closed positions don't
    leave orphan rows behind. If ``keep_symbols`` is empty all rows are
    deleted.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    keep_symbols : set[str]
        Symbols that must remain in the table.

    Returns
    -------
    int
        Number of rows deleted.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        if not keep_symbols:
            cursor = await conn.execute("DELETE FROM position_entry")
        else:
            placeholders = ",".join("?" for _ in keep_symbols)
            cursor = await conn.execute(
                f"DELETE FROM position_entry WHERE symbol NOT IN ({placeholders})",
                tuple(keep_symbols),
            )
        await conn.commit()
        return cursor.rowcount


async def lookup_blacklist(data_dir: Path, symbol: str) -> BlacklistEntry | None:
    """Look up a single symbol in the blacklist.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    symbol : str
        Ticker to look up (case-insensitive).

    Returns
    -------
    BlacklistEntry | None
        The matching entry or ``None``.
    """
    sym = symbol.upper().strip()
    if not sym:
        return None
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT symbol, loss_date, expires FROM blacklist WHERE symbol = ?",
            (sym,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return BlacklistEntry(
        symbol=row["symbol"],
        loss_date=datetime.date.fromisoformat(row["loss_date"]),
        expires=datetime.date.fromisoformat(row["expires"]),
    )


# ---------------------------------------------------------------------------
# API key persistence
# ---------------------------------------------------------------------------


def _row_to_api_key(row: aiosqlite.Row) -> ApiKey:
    """Convert a SQLite row to an ApiKey dataclass."""
    last_used_raw = row["last_used_at"]
    return ApiKey(
        id=row["id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        key_hash=row["key_hash"],
        created_at=datetime.datetime.fromisoformat(row["created_at"]),
        last_used_at=(
            datetime.datetime.fromisoformat(last_used_raw) if last_used_raw else None
        ),
    )


async def insert_api_key(
    data_dir: Path,
    *,
    name: str,
    key_prefix: str,
    key_hash: str,
) -> ApiKey:
    """Insert a new API key row and return the persisted record.

    Parameters
    ----------
    data_dir : Path
        Application data directory.
    name : str
        User-supplied label.
    key_prefix : str
        First 8 chars of the cleartext key for identification in lists.
    key_hash : str
        SHA-256 hex digest of the cleartext key.

    Returns
    -------
    ApiKey
        The inserted row, including its assigned database id.
    """
    now = datetime.datetime.now(tz=datetime.UTC).isoformat()
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            INSERT INTO api_keys (name, key_prefix, key_hash, created_at, last_used_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (name, key_prefix, key_hash, now),
        )
        await conn.commit()
        row_id = cursor.lastrowid
        cursor = await conn.execute(
            "SELECT * FROM api_keys WHERE id = ?", (row_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        msg = "Failed to read back inserted API key row"
        raise RuntimeError(msg)
    return _row_to_api_key(row)


async def list_api_keys(data_dir: Path) -> list[ApiKey]:
    """Return all API key records ordered by creation time descending."""
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM api_keys ORDER BY created_at DESC",
        )
        rows = await cursor.fetchall()
    return [_row_to_api_key(row) for row in rows]


async def delete_api_key(data_dir: Path, key_id: int) -> bool:
    """Delete a single API key by id.

    Returns
    -------
    bool
        True if a row was deleted.
    """
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        cursor = await conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def lookup_api_key_by_hash(
    data_dir: Path,
    key_hash: str,
) -> ApiKey | None:
    """Find an API key by its SHA-256 hash."""
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,),
        )
        row = await cursor.fetchone()
    return _row_to_api_key(row) if row else None


async def touch_api_key(data_dir: Path, key_id: int) -> None:
    """Update ``last_used_at`` on an API key row to now (UTC)."""
    now = datetime.datetime.now(tz=datetime.UTC).isoformat()
    async with aiosqlite.connect(db_path(data_dir)) as conn:
        await conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (now, key_id),
        )
        await conn.commit()

