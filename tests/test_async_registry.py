"""Tests for sky_claw.antigravity.db.async_registry.

Uses the global `async_registry` fixture from conftest.py — demonstrates
the centralized M-01-compliant registry setup pattern.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sqlite3
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.antigravity.db.async_registry import AsyncModRegistry, _DatabaseCorruptionError


class _CursorConFallo:
    """Cursor minimo para inyectar un fallo real durante quick_check."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> _CursorConFallo:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    async def fetchone(self) -> None:
        raise self._error


class TestAsyncSchemaCreation:
    @pytest.mark.asyncio
    async def test_tables_exist(self, async_registry: AsyncModRegistry) -> None:
        assert async_registry._conn is not None
        async with async_registry._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
            tables = {row[0] for row in rows}
        assert {"mods", "dependencies", "task_log"} <= tables


class TestAsyncUpsert:
    @pytest.mark.asyncio
    async def test_upsert_single(self, async_registry: AsyncModRegistry) -> None:
        mod_id = await async_registry.upsert_mod(nexus_id=1234, name="SKSE", version="2.2.6")
        assert mod_id >= 1
        row = await async_registry.get_mod(1234)
        assert row is not None
        assert row[2] == "SKSE"  # name column

    @pytest.mark.asyncio
    async def test_upsert_updates(self, async_registry: AsyncModRegistry) -> None:
        await async_registry.upsert_mod(nexus_id=100, name="SkyUI", version="5.1")
        await async_registry.upsert_mod(nexus_id=100, name="SkyUI", version="5.2")
        row = await async_registry.get_mod(100)
        assert row is not None
        assert row[3] == "5.2"  # version column

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, async_registry: AsyncModRegistry) -> None:
        assert await async_registry.get_mod(99999) is None


class TestMicroBatching:
    @pytest.mark.asyncio
    async def test_upsert_mods_batch(self, async_registry: AsyncModRegistry) -> None:
        rows = [
            (1001, "ModA", "1.0", "auth1", "cat1", "", False, False),
            (1002, "ModB", "2.0", "auth2", "cat2", "", False, False),
            (1003, "ModC", "3.0", "auth3", "cat3", "", False, False),
        ]
        await async_registry.upsert_mods_batch(rows)
        for nexus_id, _name, *_ in rows:
            row = await async_registry.get_mod(nexus_id)
            assert row is not None

    @pytest.mark.asyncio
    async def test_upsert_mods_batch_empty(self, async_registry: AsyncModRegistry) -> None:
        await async_registry.upsert_mods_batch([])  # should not raise

    @pytest.mark.asyncio
    async def test_insert_deps_batch(self, async_registry: AsyncModRegistry) -> None:
        mod_id = await async_registry.upsert_mod(nexus_id=2000, name="DepHost")
        deps = [
            (mod_id, 3001, "DepA"),
            (mod_id, 3002, "DepB"),
        ]
        await async_registry.insert_deps_batch(deps)
        assert async_registry._conn is not None
        async with async_registry._conn.execute("SELECT * FROM dependencies WHERE mod_id = ?", (mod_id,)) as cur:
            found = await cur.fetchall()
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_log_tasks_batch(self, async_registry: AsyncModRegistry) -> None:
        logs = [
            (None, "sync", "ok", "ModA"),
            (None, "sync", "error", "ModB: timeout"),
        ]
        await async_registry.log_tasks_batch(logs)
        assert async_registry._conn is not None
        async with async_registry._conn.execute("SELECT * FROM task_log") as cur:
            found = await cur.fetchall()
        assert len(found) == 2


class TestOwnershipEnFallosDeArranque:
    @pytest.mark.asyncio
    async def test_quick_check_ordinario_conserva_conexion_hasta_shutdown(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "quick_check_fallido.db"
        lifecycle = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn = await lifecycle.get_connection(db_path)
        path_key = str(db_path.resolve())
        registry = AsyncModRegistry(db_path, lifecycle=lifecycle)
        error_quick_check = sqlite3.OperationalError("quick_check deliberadamente fallido")

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    conn,
                    "execute",
                    MagicMock(return_value=_CursorConFallo(error_quick_check)),
                )

                with pytest.raises(sqlite3.OperationalError) as exc_info:
                    await registry.open()

            assert exc_info.value is error_quick_check
            assert lifecycle._connections.get(path_key) is conn
            assert registry._conn is None

            await lifecycle.shutdown_all()
            assert lifecycle.managed_paths == []
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cierre",
        [
            pytest.param(OSError("close deliberadamente fallido"), id="error"),
            pytest.param(asyncio.CancelledError("close deliberadamente cancelado"), id="cancelacion"),
        ],
    )
    async def test_corrupcion_con_close_incompleto_conserva_owner_y_error_primario(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        error_cierre: BaseException,
    ) -> None:
        db_path = tmp_path / "corrupcion_close_fallido.db"
        lifecycle = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn = await lifecycle.get_connection(db_path)
        path_key = str(db_path.resolve())
        registry = AsyncModRegistry(db_path, lifecycle=lifecycle)
        error_corrupcion = _DatabaseCorruptionError("corrupcion deliberada")
        cierre_intentos = 0
        close_real = conn.close

        async def cerrar_con_fallo_inicial() -> None:
            nonlocal cierre_intentos
            cierre_intentos += 1
            if cierre_intentos == 1:
                raise error_cierre
            await close_real()

        get_connection_spy = AsyncMock(wraps=lifecycle.get_connection)
        rename_spy = MagicMock(side_effect=AssertionError("no debe renombrar"))
        monkeypatch.setattr(conn, "close", cerrar_con_fallo_inicial)
        monkeypatch.setattr(lifecycle, "get_connection", get_connection_spy)
        monkeypatch.setattr(pathlib.Path, "rename", rename_spy)

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    conn,
                    "execute",
                    MagicMock(return_value=_CursorConFallo(error_corrupcion)),
                )

                with pytest.raises(_DatabaseCorruptionError) as exc_info:
                    await registry.open()

            assert exc_info.value is error_corrupcion
            assert exc_info.value.__cause__ is error_cierre
            assert lifecycle._connections.get(path_key) is conn
            assert registry._conn is None
            get_connection_spy.assert_awaited_once_with(str(db_path))
            rename_spy.assert_not_called()

            await lifecycle.shutdown_all()

            assert lifecycle.managed_paths == []
            assert cierre_intentos == 2
        finally:
            with contextlib.suppress(Exception):
                await close_real()

    @pytest.mark.asyncio
    async def test_corrupcion_no_expulsa_reemplazo_registrado_mientras_cierra(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "corrupcion_con_reemplazo.db"
        lifecycle = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn_corrupta = await lifecycle.get_connection(db_path)
        path_key = str(db_path.resolve())
        registry = AsyncModRegistry(db_path, lifecycle=lifecycle)
        error_corrupcion = _DatabaseCorruptionError("corrupcion con reemplazo")
        cierre_iniciado = asyncio.Event()
        liberar_cierre = asyncio.Event()
        close_real = conn_corrupta.close
        conn_reemplazo = MagicMock(name="conexion_reemplazo")
        conn_reemplazo.close = AsyncMock()

        async def cerrar_bloqueado() -> None:
            cierre_iniciado.set()
            await liberar_cierre.wait()
            await close_real()

        get_connection_spy = AsyncMock(wraps=lifecycle.get_connection)
        rename_spy = MagicMock(side_effect=AssertionError("no debe renombrar"))
        monkeypatch.setattr(conn_corrupta, "close", cerrar_bloqueado)
        monkeypatch.setattr(lifecycle, "get_connection", get_connection_spy)
        monkeypatch.setattr(lifecycle, "checkpoint_all", AsyncMock(return_value={}))
        monkeypatch.setattr(pathlib.Path, "rename", rename_spy)
        open_task: asyncio.Task[None] | None = None

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    conn_corrupta,
                    "execute",
                    MagicMock(return_value=_CursorConFallo(error_corrupcion)),
                )
                open_task = asyncio.create_task(registry.open())
                await cierre_iniciado.wait()
                lifecycle._connections[path_key] = conn_reemplazo
                liberar_cierre.set()

                with pytest.raises(_DatabaseCorruptionError) as exc_info:
                    await open_task

            assert exc_info.value is error_corrupcion
            assert lifecycle._connections.get(path_key) is conn_reemplazo
            assert registry._conn is None
            get_connection_spy.assert_awaited_once_with(str(db_path))
            rename_spy.assert_not_called()
            conn_reemplazo.close.assert_not_awaited()

            await lifecycle.shutdown_all()
            assert lifecycle.managed_paths == []
            conn_reemplazo.close.assert_awaited_once_with()
        finally:
            liberar_cierre.set()
            if open_task is not None:
                await asyncio.gather(open_task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await lifecycle.shutdown_all()
            with contextlib.suppress(Exception):
                await close_real()

    @pytest.mark.asyncio
    async def test_cancelacion_externa_durante_close_conserva_el_mismo_error_y_owner(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "corrupcion_cancelada_externamente.db"
        lifecycle = DatabaseLifecycleManager(
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
        conn = await lifecycle.get_connection(db_path)
        path_key = str(db_path.resolve())
        registry = AsyncModRegistry(db_path, lifecycle=lifecycle)
        error_corrupcion = _DatabaseCorruptionError("corrupcion antes de cancelar")
        cierre_iniciado = asyncio.Event()
        cancelacion_observada: asyncio.CancelledError | None = None
        cierre_intentos = 0
        close_real = conn.close

        async def cerrar_hasta_cancelacion() -> None:
            nonlocal cancelacion_observada, cierre_intentos
            cierre_intentos += 1
            if cierre_intentos == 1:
                cierre_iniciado.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    cancelacion_observada = error
                    raise
            await close_real()

        get_connection_spy = AsyncMock(wraps=lifecycle.get_connection)
        rename_spy = MagicMock(side_effect=AssertionError("no debe renombrar"))
        monkeypatch.setattr(conn, "close", cerrar_hasta_cancelacion)
        monkeypatch.setattr(lifecycle, "get_connection", get_connection_spy)
        monkeypatch.setattr(pathlib.Path, "rename", rename_spy)
        open_task: asyncio.Task[None] | None = None

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    conn,
                    "execute",
                    MagicMock(return_value=_CursorConFallo(error_corrupcion)),
                )
                open_task = asyncio.create_task(registry.open())
                await cierre_iniciado.wait()
                open_task.cancel("cancelacion externa deliberada")

                with pytest.raises(asyncio.CancelledError) as exc_info:
                    await open_task

            assert exc_info.value is cancelacion_observada
            assert exc_info.value.args == ("cancelacion externa deliberada",)
            assert lifecycle._connections.get(path_key) is conn
            assert registry._conn is None
            get_connection_spy.assert_awaited_once_with(str(db_path))
            rename_spy.assert_not_called()

            await lifecycle.shutdown_all()
            assert lifecycle.managed_paths == []
            assert cierre_intentos == 2
        finally:
            if open_task is not None:
                await asyncio.gather(open_task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await lifecycle.shutdown_all()
            with contextlib.suppress(Exception):
                await close_real()
