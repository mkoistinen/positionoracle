import datetime

import pytest

from positionoracle.db import (
    clear_positions,
    delete_position,
    get_setting,
    init_db,
    load_positions,
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
        assert len(loaded) == 2
        call = next(p for p in loaded if p.contract_type == ContractType.CALL)
        assert call.quantity == 20

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

    async def test_settings(self, initialized_db):
        val = await get_setting(initialized_db, "test_key")
        assert val is None

        await set_setting(initialized_db, "test_key", "test_value")
        val = await get_setting(initialized_db, "test_key")
        assert val == "test_value"

        await set_setting(initialized_db, "test_key", "updated")
        val = await get_setting(initialized_db, "test_key")
        assert val == "updated"
