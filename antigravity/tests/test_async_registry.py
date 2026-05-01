"""Tests for sky_claw.antigravity.db.async_registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sky_claw.antigravity.db.async_registry import AsyncModRegistry

if TYPE_CHECKING:
    import pathlib


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    """Provide an async registry using a temp directory."""
    registry = AsyncModRegistry(db_path=tmp_path / "test_async.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


class TestAsyncSchemaCreation:
    @pytest.mark.asyncio
    async def test_tables_exist(self, adb: AsyncModRegistry) -> None:
        assert adb._conn is not None
        async with adb._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cur:
            rows = await cur.fetchall()
            tables = {row[0] for row in rows}
        assert {"mods", "dependencies", "task_log"} <= tables


class TestAsyncUpsert:
    @pytest.mark.asyncio
    async def test_upsert_single(self, adb: AsyncModRegistry) -> None:
        mod_id = await adb.upsert_mod(nexus_id=1234, name="SKSE", version="2.2.6")
        assert mod_id >= 1
        row = await adb.get_mod(1234)
        assert row is not None
        assert row[2] == "SKSE"  # name column

    @pytest.mark.asyncio
    async def test_upsert_updates(self, adb: AsyncModRegistry) -> None:
        await adb.upsert_mod(nexus_id=100, name="SkyUI", version="5.1")
        await adb.upsert_mod(nexus_id=100, name="SkyUI", version="5.2")
        row = await adb.get_mod(100)
        assert row is not None
        assert row[3] == "5.2"  # version column

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, adb: AsyncModRegistry) -> None:
        assert await adb.get_mod(99999) is None


class TestMicroBatching:
    @pytest.mark.asyncio
    async def test_upsert_mods_batch(self, adb: AsyncModRegistry) -> None:
        rows = [
            (1001, "ModA", "1.0", "auth1", "cat1", "", False, False),
            (1002, "ModB", "2.0", "auth2", "cat2", "", False, False),
            (1003, "ModC", "3.0", "auth3", "cat3", "", False, False),
        ]
        await adb.upsert_mods_batch(rows)
        for nexus_id, _name, *_ in rows:
            row = await adb.get_mod(nexus_id)
            assert row is not None

    @pytest.mark.asyncio
    async def test_upsert_mods_batch_empty(self, adb: AsyncModRegistry) -> None:
        await adb.upsert_mods_batch([])  # should not raise

    @pytest.mark.asyncio
    async def test_insert_deps_batch(self, adb: AsyncModRegistry) -> None:
        mod_id = await adb.upsert_mod(nexus_id=2000, name="DepHost")
        deps = [
            (mod_id, 3001, "DepA"),
            (mod_id, 3002, "DepB"),
        ]
        await adb.insert_deps_batch(deps)
        assert adb._conn is not None
        async with adb._conn.execute("SELECT * FROM dependencies WHERE mod_id = ?", (mod_id,)) as cur:
            found = await cur.fetchall()
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_log_tasks_batch(self, adb: AsyncModRegistry) -> None:
        logs = [
            (None, "sync", "ok", "ModA"),
            (None, "sync", "error", "ModB: timeout"),
        ]
        await adb.log_tasks_batch(logs)
        assert adb._conn is not None
        async with adb._conn.execute("SELECT * FROM task_log") as cur:
            found = await cur.fetchall()
        assert len(found) == 2
