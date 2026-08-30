"""M-01.1: locks/router/dlq must obtain their SQLite connection from
``DatabaseLifecycleManager`` when one is injected (contrato M-01).

Direct ``aiosqlite.connect`` bypasses WAL recovery, hardened pragmas, and the
coordinated ``shutdown_all()`` checkpoint. The DI is optional: without a
lifecycle each module keeps its pre-M-01 direct-connect fallback (existing
tests construct them bare and must stay green).
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleManager
from sky_claw.app.core.dlq_manager import DLQManager
from sky_claw.app.core.event_bus import create_bus_with_dlq
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.orchestrator.rollback_factory import create_rollback_components


async def test_locks_uses_lifecycle_connection_and_does_not_close_it(tmp_path):
    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "locks.db"
        lock_mgr = DistributedLockManager(db, lifecycle=mgr)
        await lock_mgr.initialize()

        shared = await mgr.get_connection(db)
        assert lock_mgr._conn is shared

        # Lock roundtrip works on the lifecycle-managed connection.
        info = await lock_mgr.acquire_lock("res.esp", "agent-a")
        assert info.agent_id == "agent-a"
        assert await lock_mgr.release_lock("res.esp", "agent-a") is True

        # close() must NOT close the lifecycle-owned connection.
        await lock_mgr.close()
        async with shared.execute("SELECT 1") as cur:
            assert await cur.fetchone() is not None
    finally:
        await mgr.shutdown_all()


async def test_dlq_reuses_lifecycle_connection_across_operations(tmp_path):
    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "dlq.db"
        dlq = DLQManager(db_path=db, handler_resolver=lambda _name: None, lifecycle=mgr)

        assert await dlq.list_pending() == []
        key = str(db.resolve())
        assert key in mgr._connections
        shared = mgr._connections[key]

        # Second operation must reuse the SAME connection, still open.
        assert await dlq.list_dead() == []
        assert mgr._connections[key] is shared
        async with shared.execute("SELECT 1") as cur:
            assert await cur.fetchone() is not None
    finally:
        await mgr.shutdown_all()


async def test_router_uses_lifecycle_connection_and_does_not_close_it(tmp_path):
    from sky_claw.app.agent.router import LLMRouter

    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "history.db"
        router = LLMRouter(
            provider=MagicMock(),
            db_path=str(db),
            lifecycle=mgr,
        )
        await router.open()

        shared = await mgr.get_connection(db)
        assert router._conn is shared

        await router.close()
        async with shared.execute("SELECT 1") as cur:
            assert await cur.fetchone() is not None
    finally:
        await mgr.shutdown_all()


def test_rollback_factory_threads_lifecycle_to_journal_and_locks(tmp_path):
    mgr = DatabaseLifecycleManager()
    components = create_rollback_components(tmp_path / "backups", lifecycle=mgr)
    assert components.lock_manager._lifecycle is mgr
    assert components.journal._lifecycle is mgr


def test_create_bus_with_dlq_threads_lifecycle(tmp_path):
    mgr = DatabaseLifecycleManager()
    bus = create_bus_with_dlq(db_path=tmp_path / "dlq.db", lifecycle=mgr)
    assert bus._dlq._lifecycle is mgr


async def test_dlq_rolls_back_dangling_transaction_on_shared_connection(tmp_path, monkeypatch):
    """A DLQ write interrupted between its DML and ``commit()`` (e.g. the task
    cancelled during shutdown) must not leave an open transaction on the SHARED
    lifecycle connection — the next operation would inherit and commit/discard
    it. The per-op fallback got this for free (closing the connection rolls
    back); the lifecycle path must roll back explicitly.
    """
    import asyncio

    import pytest

    from sky_claw.app.core.event_bus import Event

    mgr = DatabaseLifecycleManager()
    try:
        db = tmp_path / "dlq.db"
        dlq = DLQManager(db_path=db, handler_resolver=lambda _n: None, lifecycle=mgr)
        await dlq.list_pending()  # ensure schema + register the shared connection
        shared = mgr._connections[str(db.resolve())]

        async def _cancelled_commit():
            raise asyncio.CancelledError

        monkeypatch.setattr(shared, "commit", _cancelled_commit)
        with pytest.raises(asyncio.CancelledError):
            await dlq.enqueue(Event(topic="t.fail", payload={"k": "v"}), lambda _e: None, RuntimeError("x"))
        monkeypatch.undo()

        assert shared.in_transaction is False, "dangling transaction left on the shared connection"
        # The discarded row must not resurface in a later (healthy) operation.
        assert await dlq.list_pending() == []
    finally:
        await mgr.shutdown_all()


# =============================================================================
# PR-1b2a (wiring): TODO DistributedLockManager productivo recibe lifecycle=
# =============================================================================

_RAIZ_REPO = Path(__file__).resolve().parents[1]

#: Inventario EXACTO de construcciones productivas de DistributedLockManager.
#: Clave: (archivo relativo, función envolvente, variable asignada). La igualdad
#: exhaustiva rompe el test si aparece un TERCER constructor no enumerado; y un
#: sitio enumerado sin ``lifecycle=`` rompe la verificación de keyword.
_SITIOS_LOCK_MANAGER_PRODUCTIVOS: set[tuple[str, str, str]] = {
    ("sky_claw/app_context.py", "_start_full_inner", "tools_installer_lock_manager"),
    ("sky_claw/app_context.py", "_start_full_inner", "lock_manager"),
    ("sky_claw/app/orchestrator/rollback_factory.py", "create_rollback_components", "lock_manager"),
}


def test_inventario_productivo_de_lock_managers_exige_lifecycle() -> None:
    """Inventario de wiring, no de comportamiento (eso lo cubren las carreras
    conductuales de PR-1b2a): cada construcción productiva de
    ``DistributedLockManager`` debe pasar ``lifecycle=`` explícito para
    participar del boundary. Enumeración exhaustiva por AST: un sitio nuevo
    rompe la igualdad del inventario hasta ser enumerado, y enumerado sin
    ``lifecycle`` rompe la verificación de keyword."""
    encontrados: dict[tuple[str, str, str], bool] = {}
    for archivo in (_RAIZ_REPO / "sky_claw").rglob("*.py"):
        rel = str(archivo.relative_to(_RAIZ_REPO)).replace("\\", "/")
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        padres: dict[ast.AST, ast.AST] = {}
        for nodo in ast.walk(arbol):
            for hijo in ast.iter_child_nodes(nodo):
                padres[hijo] = nodo

        for nodo in ast.walk(arbol):
            if not (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id == "DistributedLockManager"
            ):
                continue
            variable: str | None = None
            funcion: str | None = None
            cur: ast.AST | None = padres.get(nodo)
            while cur is not None:
                if variable is None and isinstance(cur, ast.Assign):
                    for target in cur.targets:
                        if isinstance(target, ast.Name):
                            variable = target.id
                            break
                if variable is None and isinstance(cur, ast.AnnAssign) and isinstance(cur.target, ast.Name):
                    variable = cur.target.id
                if funcion is None and isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcion = cur.name
                cur = padres.get(cur)

            tiene_lifecycle = any(isinstance(kw, ast.keyword) and kw.arg == "lifecycle" for kw in nodo.keywords)
            encontrados[(rel, funcion or "<module>", variable or "<sin-variable>")] = tiene_lifecycle

    assert set(encontrados) == _SITIOS_LOCK_MANAGER_PRODUCTIVOS, (
        f"inventario de construcciones productivas de DistributedLockManager cambiado: "
        f"{set(encontrados) ^ _SITIOS_LOCK_MANAGER_PRODUCTIVOS}"
    )
    sin_lifecycle = [sitio for sitio, tiene in encontrados.items() if not tiene]
    assert not sin_lifecycle, f"constructores productivos sin lifecycle=: {sin_lifecycle}"
