"""Tests for database integrity checks and index creation."""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.db.registry import ModRegistry


class TestAsyncRegistryIntegrity:
    @pytest.mark.asyncio
    async def test_open_creates_index(self, tmp_path: pathlib.Path) -> None:
        db = AsyncModRegistry(tmp_path / "test.db")
        await db.open()
        try:
            assert db._conn is not None
            async with db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_mods_name'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_corrupt_db_raises(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a valid sqlite database")

        db = AsyncModRegistry(db_path)
        with pytest.raises((RuntimeError, Exception)):
            await db.open()


class TestSyncRegistryIntegrity:
    @pytest.mark.asyncio
    async def test_open_creates_index(self, tmp_path: pathlib.Path) -> None:
        db = ModRegistry(tmp_path / "test.db")
        await db.open()
        try:
            assert db._conn is not None
            async with db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_mods_name'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_corrupt_db_raises(self, tmp_path: pathlib.Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a valid sqlite database")

        db = ModRegistry(db_path)
        with pytest.raises((RuntimeError, Exception)):
            await db.open()
