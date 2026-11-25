"""La conversión a WAL reintenta en TODO camino que abre SQLite por su cuenta.

``PRAGMA journal_mode=WAL`` sobre un archivo que no está en WAL promueve el lock a
EXCLUSIVE. Si otra conexión está en medio de la misma promoción (dos procesos abriendo
una base nueva a la vez), SQLite le devuelve SQLITE_BUSY al perdedor AL INSTANTE, sin
pasar por el busy handler: ningún ``busy_timeout`` lo salva. ``DatabaseLifecycleManager``
lo resolvía con un reintento acotado; los caminos standalone (sin lifecycle) corrían el
pragma pelado y morían con ``database is locked``. Así se cayó ``main`` en Windows
(run 45677510174): dos procesos inicializando el mismo ``DistributedLockManager``.

Propiedad: *la conversión a WAL solo es robusta si TODA apertura pasa por el mismo
reintento*. Dos anclas:

- por AST, el pragma ``journal_mode=WAL`` solo se ejecuta dentro de
  ``db_lifecycle.convertir_a_wal``: una apertura nueva con el pragma pelado rompe el test;
- los módulos que llaman a ``convertir_a_wal`` se congelan contra las recetas de abajo:
  un llamador nuevo rompe el test hasta tener su receta, y la receta lo mete en el test
  de comportamiento con SQLite real (el holder de ``test_db_lifecycle.py``, que deja el
  lock exacto de la carrera sin simular ningún error).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite
import pytest

from tests.test_db_lifecycle import _sembrar_db_en_modo_delete, _SostieneLockSQLite

RAIZ = pathlib.Path(__file__).resolve().parents[1]
_PRAGMA_WAL = re.compile(r"^\s*pragma\s+journal_mode\s*=\s*wal\s*;?\s*$", re.IGNORECASE)


async def _abrir_locks(db: pathlib.Path) -> None:
    from sky_claw.app.db.locks import DistributedLockManager

    manager = DistributedLockManager(db, default_ttl=30.0)
    await manager.initialize()
    await manager.close()


async def _abrir_historial_del_router(db: pathlib.Path) -> None:
    from sky_claw.app.agent.router import LLMRouter

    router = LLMRouter(provider=object(), db_path=str(db))  # type: ignore[arg-type]
    await router.open()
    await router.close()


async def _abrir_registro_async(db: pathlib.Path) -> None:
    from sky_claw.app.db.async_registry import AsyncModRegistry

    registro = AsyncModRegistry(db)
    await registro.open()
    await registro.close()


async def _abrir_registro(db: pathlib.Path) -> None:
    from sky_claw.app.db.registry import ModRegistry

    registro = ModRegistry(db)
    await registro.open()
    await registro.close()


async def _abrir_journal(db: pathlib.Path) -> None:
    from sky_claw.app.db.journal import OperationJournal

    journal = OperationJournal(db)
    await journal.open()
    await journal.close()


async def _abrir_dlq(db: pathlib.Path) -> None:
    from sky_claw.app.core.dlq_manager import DLQManager

    dlq = DLQManager(db, handler_resolver=lambda _nombre: None)
    await dlq._ensure_schema()


async def _abrir_vault(db: pathlib.Path) -> None:
    from sky_claw.app.security.credential_vault import _SQLitePool

    pool = _SQLitePool(str(db))
    async with pool.acquire() as conn:
        assert isinstance(conn, aiosqlite.Connection)
    await pool.close()


#: Módulo que convierte a WAL por su cuenta → cómo abrirlo sin lifecycle.
RECETAS: dict[str, Callable[[pathlib.Path], Awaitable[None]]] = {
    "sky_claw/app/agent/router.py": _abrir_historial_del_router,
    "sky_claw/app/core/dlq_manager.py": _abrir_dlq,
    "sky_claw/app/db/async_registry.py": _abrir_registro_async,
    "sky_claw/app/db/journal.py": _abrir_journal,
    "sky_claw/app/db/locks.py": _abrir_locks,
    "sky_claw/app/db/registry.py": _abrir_registro,
    "sky_claw/app/security/credential_vault.py": _abrir_vault,
}


def _modulos_de_produccion() -> list[pathlib.Path]:
    return sorted((RAIZ / "sky_claw").rglob("*.py"))


def _relativo(ruta: pathlib.Path) -> str:
    return ruta.relative_to(RAIZ).as_posix()


def _funcion_que_contiene(arbol: ast.Module) -> dict[ast.AST, str]:
    duenio: dict[ast.AST, str] = {}
    for funcion in ast.walk(arbol):
        if isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
            for nodo in ast.walk(funcion):
                duenio[nodo] = funcion.name  # la más interna gana: ast.walk es BFS
    return duenio


def _ejecuciones_del_pragma_wal() -> set[tuple[str, str]]:
    """(módulo, función) de toda llamada cuyo primer argumento es el pragma de conversión."""
    encontradas: set[tuple[str, str]] = set()
    for ruta in _modulos_de_produccion():
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        duenio = _funcion_que_contiene(arbol)
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not nodo.args:
                continue
            primero = nodo.args[0]
            es_pragma = isinstance(primero, ast.Constant) and isinstance(primero.value, str)
            if es_pragma and _PRAGMA_WAL.match(primero.value):
                encontradas.add((_relativo(ruta), duenio.get(nodo, "<módulo>")))
    return encontradas


def _llamadores_de_convertir_a_wal() -> set[str]:
    llamadores: set[str] = set()
    for ruta in _modulos_de_produccion():
        for nodo in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else getattr(nodo.func, "id", None)
            if nombre == "convertir_a_wal":
                llamadores.add(_relativo(ruta))
    return llamadores


def test_el_detector_ve_el_pragma_en_sus_formas_reales() -> None:
    """El ancla no puede pasar por no ver nada: reconoce las grafías que hay en el repo."""
    for grafia in ("PRAGMA journal_mode=WAL", "PRAGMA journal_mode=WAL;", "pragma journal_mode = wal"):
        assert _PRAGMA_WAL.match(grafia), grafia
    assert not _PRAGMA_WAL.match("PRAGMA journal_mode")
    assert not _PRAGMA_WAL.match("PRAGMA journal_mode=DELETE")


def test_el_pragma_de_conversion_solo_se_ejecuta_dentro_del_reintento() -> None:
    assert _ejecuciones_del_pragma_wal() == {("sky_claw/app/core/db_lifecycle.py", "convertir_a_wal")}


def test_todo_llamador_del_reintento_tiene_receta() -> None:
    """Un módulo nuevo que abre SQLite por su cuenta entra a la tabla de comportamiento."""
    llamadores = _llamadores_de_convertir_a_wal() - {"sky_claw/app/core/db_lifecycle.py"}
    assert llamadores == set(RECETAS)


@pytest.mark.parametrize("modulo", sorted(RECETAS))
async def test_la_apertura_standalone_espera_a_la_conversion_rival(tmp_path: pathlib.Path, modulo: str) -> None:
    """Con otra conexión en medio de la promoción, la apertura reintenta y gana al liberarse.

    Sin el reintento muere al instante con ``database is locked``: SQLite saltea el busy
    handler en la promoción de lock.
    """
    # Arrange: base legada en modo DELETE y una conexión real sosteniendo RESERVED.
    db = tmp_path / "base.db"
    _sembrar_db_en_modo_delete(db)
    holder = _SostieneLockSQLite(db)
    holder.start()
    assert holder.adquirido.wait(5), "el holder real no consiguió su lock"

    async def _liberar_tarde() -> None:
        await asyncio.sleep(0.3)
        holder.liberar.set()

    liberador = asyncio.create_task(_liberar_tarde())
    try:
        # Act
        await RECETAS[modulo](db)
    finally:
        liberador.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await liberador
        holder.liberar.set()
        holder.join(5)

    # Assert: la base quedó en WAL.
    async with aiosqlite.connect(db) as conn, conn.execute("PRAGMA journal_mode") as cursor:
        fila: Any = await cursor.fetchone()
    assert fila[0] == "wal"
