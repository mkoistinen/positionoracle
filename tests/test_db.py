import datetime

import pytest

from positionoracle.db import (
    bulk_upsert_blacklist,
    clear_positions,
    delete_expired_positions,
    delete_position,
    get_setting,
    init_db,
    load_blacklist,
    load_positions,
    lookup_blacklist,
    prune_blacklist,
    set_setting,
    upsert_positions,
)
from positionoracle.types import ContractType, Position


@pytest.fixture
async def initialized_db(data_dir):
    await init_db(data_dir)
    return data_dir


def _sample_positions():
    return [
        Position(
            symbol="AAPL251219C00150000",
            underlying="AAPL",
            contract_type=ContractType.CALL,
            strike=150.0,
            expiration=datetime.date(2025, 12, 19),
            quantity=10,
            cost_basis=5000.0,
        ),
        Position(
            symbol="AAPL251219P00140000",
            underlying="AAPL",
            contract_type=ContractType.PUT,
            strike=140.0,
            expiration=datetime.date(2025, 12, 19),
            quantity=-5,
            cost_basis=-2500.0,
        ),
    ]


class TestDatabase:
    async def test_init_creates_tables(self, data_dir):
        await init_db(data_dir)
        positions = await load_positions(data_dir)
        assert positions == []

    async def test_upsert_and_load(self, initialized_db):
        positions = _sample_positions()
        count = await upsert_positions(initialized_db, positions)
        assert count == 2

        loaded = await load_positions(initialized_db)
        assert len(loaded) == 2

    async def test_upsert_updates_existing(self, initialized_db):
        positions = _sample_positions()
        await upsert_positions(initialized_db, positions)

        updated = [
            Position(
                symbol="AAPL251219C00150000",
                underlying="AAPL",
                contract_type=ContractType.CALL,
                strike=150.0,
                expiration=datetime.date(2025, 12, 19),
                quantity=20,
                cost_basis=10000.0,
            ),
        ]
        await upsert_positions(initialized_db, updated)

        loaded = await load_positions(initialized_db)
        # upsert_positions replaces the full set — stale positions are removed
        assert len(loaded) == 1
        assert loaded[0].quantity == 20

    async def test_delete_position(self, initialized_db):
        await upsert_positions(initialized_db, _sample_positions())
        deleted = await delete_position(initialized_db, "AAPL251219C00150000")
        assert deleted
        loaded = await load_positions(initialized_db)
        assert len(loaded) == 1

    async def test_delete_nonexistent(self, initialized_db):
        deleted = await delete_position(initialized_db, "NOSUCH")
        assert not deleted

    async def test_clear_positions(self, initialized_db):
        await upsert_positions(initialized_db, _sample_positions())
        count = await clear_positions(initialized_db)
        assert count == 2
        loaded = await load_positions(initialized_db)
        assert loaded == []

    async def test_delete_expired_positions(self, initialized_db):
        expired_call = Position(
            symbol="AAPL240119C00150000",
            underlying="AAPL",
            contract_type=ContractType.CALL,
            strike=150.0,
            expiration=datetime.date(2024, 1, 19),
            quantity=1,
            cost_basis=100.0,
        )
        future_put = Position(
            symbol="AAPL991219P00140000",
            underlying="AAPL",
            contract_type=ContractType.PUT,
            strike=140.0,
            expiration=datetime.date(2099, 12, 19),
            quantity=-1,
            cost_basis=-50.0,
        )
        stock = Position(
            symbol="AAPL",
            underlying="AAPL",
            contract_type=ContractType.STOCK,
            strike=0.0,
            expiration=datetime.date.max,
            quantity=100,
            cost_basis=14700.0,
            multiplier=1,
        )
        await upsert_positions(
            initialized_db, [expired_call, future_put, stock],
        )

        deleted = await delete_expired_positions(
            initialized_db, datetime.date(2026, 5, 15),
        )
        assert deleted == 1

        loaded = await load_positions(initialized_db)
        symbols = {p.symbol for p in loaded}
        assert "AAPL240119C00150000" not in symbols
        assert "AAPL991219P00140000" in symbols
        assert "AAPL" in symbols

    async def test_delete_expired_keeps_today(self, initialized_db):
        today = datetime.date(2026, 5, 15)
        expiring_today = Position(
            symbol="SPY260515C00500000",
            underlying="SPY",
            contract_type=ContractType.CALL,
            strike=500.0,
            expiration=today,
            quantity=1,
            cost_basis=10.0,
        )
        await upsert_positions(initialized_db, [expiring_today])

        deleted = await delete_expired_positions(initialized_db, today)
        assert deleted == 0
        loaded = await load_positions(initialized_db)
        assert len(loaded) == 1

    async def test_delete_expired_empty_db(self, initialized_db):
        deleted = await delete_expired_positions(
            initialized_db, datetime.date(2026, 5, 15),
        )
        assert deleted == 0

    async def test_blacklist_upsert_and_load(self, initialized_db):
        await bulk_upsert_blacklist(initialized_db, [
            ("AAPL", datetime.date(2026, 4, 1)),
            ("MSFT", datetime.date(2026, 4, 15)),
        ])
        entries = await load_blacklist(initialized_db)
        symbols = [e.symbol for e in entries]
        assert symbols == ["AAPL", "MSFT"]  # sorted by expires ASC
        assert entries[0].expires == datetime.date(2026, 5, 1)
        assert entries[1].expires == datetime.date(2026, 5, 15)

    async def test_blacklist_normalizes_symbol_case(self, initialized_db):
        await bulk_upsert_blacklist(initialized_db, [
            ("aapl", datetime.date(2026, 4, 1)),
        ])
        entries = await load_blacklist(initialized_db)
        assert entries[0].symbol == "AAPL"

    async def test_blacklist_keeps_most_recent_loss_date(self, initialized_db):
        # First write: April 1.
        await bulk_upsert_blacklist(initialized_db, [
            ("AAPL", datetime.date(2026, 4, 1)),
        ])
        # Second write with older date — should NOT overwrite.
        await bulk_upsert_blacklist(initialized_db, [
            ("AAPL", datetime.date(2026, 3, 1)),
        ])
        entries = await load_blacklist(initialized_db)
        assert entries[0].loss_date == datetime.date(2026, 4, 1)

        # Third write with newer date — SHOULD overwrite.
        await bulk_upsert_blacklist(initialized_db, [
            ("AAPL", datetime.date(2026, 4, 20)),
        ])
        entries = await load_blacklist(initialized_db)
        assert entries[0].loss_date == datetime.date(2026, 4, 20)
        assert entries[0].expires == datetime.date(2026, 5, 20)

    async def test_blacklist_prune(self, initialized_db):
        await bulk_upsert_blacklist(initialized_db, [
            ("EXPIRED", datetime.date(2025, 1, 1)),  # expires 2025-01-31
            ("ACTIVE", datetime.date(2026, 5, 1)),   # expires 2026-05-31
        ])
        pruned = await prune_blacklist(initialized_db, datetime.date(2026, 5, 16))
        assert pruned == 1
        remaining = await load_blacklist(initialized_db)
        assert [e.symbol for e in remaining] == ["ACTIVE"]

    async def test_blacklist_lookup(self, initialized_db):
        await bulk_upsert_blacklist(initialized_db, [
            ("AAPL", datetime.date(2026, 4, 1)),
        ])
        hit = await lookup_blacklist(initialized_db, "aapl")
        assert hit is not None
        assert hit.symbol == "AAPL"
        miss = await lookup_blacklist(initialized_db, "TSLA")
        assert miss is None

    async def test_blacklist_empty_bulk_is_noop(self, initialized_db):
        n = await bulk_upsert_blacklist(initialized_db, [])
        assert n == 0
        assert await load_blacklist(initialized_db) == []

    async def test_settings(self, initialized_db):
        val = await get_setting(initialized_db, "test_key")
        assert val is None

        await set_setting(initialized_db, "test_key", "test_value")
        val = await get_setting(initialized_db, "test_key")
        assert val == "test_value"

        await set_setting(initialized_db, "test_key", "updated")
        val = await get_setting(initialized_db, "test_key")
        assert val == "updated"
