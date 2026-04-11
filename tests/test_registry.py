"""Tests for sky_claw.db.registry."""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.db.registry import ModRegistry


@pytest.fixture()
async def db(tmp_path: pathlib.Path) -> ModRegistry:
    """Provide an in-memory-like registry using a temp directory."""
    registry = ModRegistry(db_path=tmp_path / "test_mod_registry.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


# ------------------------------------------------------------------
# Schema creation
# ------------------------------------------------------------------


class TestSchemaCreation:
    @pytest.mark.asyncio
    async def test_tables_exist(self, db: ModRegistry) -> None:
        assert db._conn is not None
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        tables = {row[0] for row in rows}
        assert {"mods", "dependencies", "task_log"} <= tables


# ------------------------------------------------------------------
# Mod CRUD
# ------------------------------------------------------------------


class TestModCRUD:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, db: ModRegistry) -> None:
        mod_id = await db.upsert_mod(
            nexus_id=1234, name="SKSE", version="2.2.6", author="ianpatt"
        )
        assert mod_id >= 1
        row = await db.get_mod(1234)
        assert row is not None
        assert row["name"] == "SKSE"
        assert row["version"] == "2.2.6"

    @pytest.mark.asyncio
    async def test_upsert_updates_version(self, db: ModRegistry) -> None:
        await db.upsert_mod(nexus_id=100, name="SkyUI", version="5.1")
        await db.upsert_mod(nexus_id=100, name="SkyUI", version="5.2")
        row = await db.get_mod(100)
        assert row is not None
        assert row["version"] == "5.2"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db: ModRegistry) -> None:
        assert await db.get_mod(99999) is None

    @pytest.mark.asyncio
    async def test_set_vfs_status(self, db: ModRegistry) -> None:
        await db.upsert_mod(nexus_id=200, name="TestMod")
        await db.set_vfs_status(200, installed=True, enabled=True)
        row = await db.get_mod(200)
        assert row is not None
        assert row["installed"] == 1
        assert row["enabled_in_vfs"] == 1


# ------------------------------------------------------------------
# Dependencies
# ------------------------------------------------------------------


class TestDependencies:
    @pytest.mark.asyncio
    async def test_add_and_get_dependency(self, db: ModRegistry) -> None:
        mod_id = await db.upsert_mod(nexus_id=300, name="DepMod")
        await db.add_dependency(mod_id, depends_on_nexus_id=1234, dep_name="SKSE")
        deps = await db.get_dependencies(mod_id)
        assert len(deps) == 1
        assert deps[0]["depends_on_nexus_id"] == 1234

    @pytest.mark.asyncio
    async def test_duplicate_dependency_ignored(self, db: ModRegistry) -> None:
        mod_id = await db.upsert_mod(nexus_id=301, name="Mod2")
        await db.add_dependency(mod_id, depends_on_nexus_id=1234)
        await db.add_dependency(mod_id, depends_on_nexus_id=1234)
        deps = await db.get_dependencies(mod_id)
        assert len(deps) == 1


# ------------------------------------------------------------------
# Task log
# ------------------------------------------------------------------


class TestTaskLog:
    @pytest.mark.asyncio
    async def test_log_task(self, db: ModRegistry) -> None:
        mod_id = await db.upsert_mod(nexus_id=400, name="LogMod")
        log_id = await db.log_task("download", mod_id=mod_id, status="ok")
        assert log_id >= 1

    @pytest.mark.asyncio
    async def test_log_without_mod(self, db: ModRegistry) -> None:
        log_id = await db.log_task("system_start")
        assert log_id >= 1
