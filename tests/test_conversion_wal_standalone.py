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
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite
import pytest

from tests.test_db_lifecycle import _sembrar_db_en_modo_delete, _SostieneLockSQLite

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Marcador para un interpolado que no se pudo resolver estáticamente.
_MARCA_INTERPOLACION = "\x00VAL\x00"
_SENTENCIAS_SQL = frozenset({"execute", "executemany", "executescript"})
_NOMBRES_SQL_KW = frozenset({"sql", "query", "statement", "script"})
_PRAGMA_WAL_SENTENCIA = re.compile(r"^\s*pragma\s+(?:main\.)?journal_mode\s*=\s*(\S+?)\s*;?\s*$", re.IGNORECASE)


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


def _texto_de_expresion(nodo: ast.AST, constantes: dict[str, str]) -> str | None:
    """Resuelve literales, constantes, concatenaciones y f-strings acotados."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if isinstance(nodo, ast.Name):
        return constantes.get(nodo.id)
    if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name):
        return constantes.get(f"{nodo.value.id}.{nodo.attr}")
    if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Add):
        izquierda = _texto_de_expresion(nodo.left, constantes)
        derecha = _texto_de_expresion(nodo.right, constantes)
        return izquierda + derecha if izquierda is not None and derecha is not None else None
    if isinstance(nodo, ast.JoinedStr):
        partes: list[str] = []
        for parte in nodo.values:
            if isinstance(parte, ast.Constant) and isinstance(parte.value, str):
                partes.append(parte.value)
            elif isinstance(parte, ast.FormattedValue):
                resuelto = _texto_de_expresion(parte.value, constantes)
                partes.append(resuelto if resuelto is not None else _MARCA_INTERPOLACION)
        return "".join(partes)
    return None


def _constantes_de_modulo(arbol: ast.Module) -> dict[str, str]:
    """Censo de asignaciones de texto del módulo; resolución acotada (3 pasadas)."""
    candidatos: dict[str, ast.AST] = {}
    for nodo in ast.walk(arbol):
        objetivo = None
        if isinstance(nodo, ast.Assign) and len(nodo.targets) == 1:
            objetivo = nodo.targets[0]
        elif isinstance(nodo, ast.AnnAssign):
            objetivo = nodo.target
        if objetivo is None or nodo.value is None:
            continue
        if isinstance(objetivo, ast.Name):
            candidatos[objetivo.id] = nodo.value
        elif isinstance(objetivo, ast.Attribute) and isinstance(objetivo.value, ast.Name):
            candidatos[f"{objetivo.value.id}.{objetivo.attr}"] = nodo.value
    # Una asignación dentro de ``class C`` tiene target ``SQL``, no ``C.SQL``:
    # se registran ambos nombres para no perder la forma calificada.
    for clase in ast.walk(arbol):
        if not isinstance(clase, ast.ClassDef):
            continue
        for hijo in clase.body:
            objetivo = None
            if isinstance(hijo, ast.Assign) and len(hijo.targets) == 1:
                objetivo = hijo.targets[0]
            elif isinstance(hijo, ast.AnnAssign):
                objetivo = hijo.target
            if isinstance(objetivo, ast.Name) and hijo.value is not None:
                candidatos[f"{clase.name}.{objetivo.id}"] = hijo.value
    constantes: dict[str, str] = {}
    for _ in range(3):
        for clave, valor in candidatos.items():
            texto = _texto_de_expresion(valor, constantes)
            if texto is not None:
                constantes[clave] = texto
    return constantes


def _es_conversion_wal(sql: str, constantes: dict[str, str] | None = None) -> bool:
    """Una sentencia que PONE journal_mode=wal; NO confunde lecturas ni DELETE."""
    for parte in sql.split(";"):
        encontrado = _PRAGMA_WAL_SENTENCIA.match(parte)
        if encontrado is None:
            continue
        valor = encontrado.group(1).strip().strip("'\"").lower()
        if valor == "wal":
            return True
        if (
            valor == _MARCA_INTERPOLACION.lower()
            and constantes
            and any(texto.strip("'\"").lower() == "wal" for texto in constantes.values())
        ):
            return True
    return False


def _inventario_de_codigo(codigo: str, relativo: str) -> Counter[tuple[str, str]]:
    """Cuenta los call sites de conversión WAL por (módulo, función).

    Contar —y no usar un set— es lo que detecta una segunda llamada dentro de
    un módulo ya inventariado (M11): el hermano no se derrumba en el mismo par.
    """
    arbol = ast.parse(codigo)
    duenio = _funcion_que_contiene(arbol)
    constantes = _constantes_de_modulo(arbol)
    inventario: Counter[tuple[str, str]] = Counter()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else getattr(nodo.func, "id", None)
        if nombre not in _SENTENCIAS_SQL:
            continue
        valores = list(nodo.args) + [
            palabra.value for palabra in nodo.keywords if palabra.arg is None or palabra.arg in _NOMBRES_SQL_KW
        ]
        for valor in valores:
            texto = _texto_de_expresion(valor, constantes)
            if texto is not None and _es_conversion_wal(texto, constantes):
                inventario[(relativo, duenio.get(nodo, "<módulo>"))] += 1
                break
    return inventario


def _modulos_analizados() -> list[pathlib.Path]:
    """La propiedad alcanza producción, scripts y local_scripts (no tests)."""
    return sorted(
        ruta
        for base in (RAIZ / "sky_claw", RAIZ / "scripts", RAIZ / "local_scripts")
        if base.is_dir()
        for ruta in base.rglob("*.py")
    )


def _ejecuciones_del_pragma_wal() -> Counter[tuple[str, str]]:
    inventario: Counter[tuple[str, str]] = Counter()
    for ruta in _modulos_analizados():
        inventario.update(_inventario_de_codigo(ruta.read_text(encoding="utf-8"), _relativo(ruta)))
    return inventario


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
        assert _es_conversion_wal(grafia), grafia
    assert not _es_conversion_wal("PRAGMA journal_mode")
    assert not _es_conversion_wal("PRAGMA journal_mode=DELETE")


def test_el_pragma_de_conversion_solo_se_ejecuta_dentro_del_reintento() -> None:
    assert _ejecuciones_del_pragma_wal() == Counter({("sky_claw/app/core/db_lifecycle.py", "convertir_a_wal"): 1})


@pytest.mark.parametrize(
    ("nombre", "codigo", "esperado"),
    [
        (
            "main_calificado",
            'async def f(conn):\n    await conn.execute("PRAGMA main.journal_mode=WAL")\n',
            1,
        ),
        (
            "executescript",
            'async def f(conn):\n    await conn.executescript("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;")\n',
            1,
        ),
        (
            "keyword_sql",
            'async def f(conn):\n    await conn.execute(sql="PRAGMA journal_mode=WAL")\n',
            1,
        ),
        (
            "constante",
            '_SQL = "PRAGMA journal_mode=WAL"\nasync def f(conn):\n    await conn.execute(_SQL)\n',
            1,
        ),
        (
            "constante_de_clase",
            'class C:\n    SQL = "PRAGMA journal_mode=WAL"\nasync def f(conn):\n    await conn.execute(C.SQL)\n',
            1,
        ),
        (
            "fstring_literal",
            'async def f(conn):\n    await conn.execute(f"PRAGMA journal_mode=WAL")\n',
            1,
        ),
        (
            "fstring_con_variable_resuelta",
            'async def f(conn):\n    modo = "WAL"\n    await conn.execute(f"PRAGMA journal_mode={modo}")\n',
            1,
        ),
        (
            "llamada_duplicada",
            'async def f(conn):\n    await conn.execute("PRAGMA journal_mode=WAL")\n'
            '    await conn.execute("PRAGMA journal_mode=WAL")\n',
            2,
        ),
    ],
)
def test_el_ancla_detecta_las_evasiones_reales(nombre: str, codigo: str, esperado: int) -> None:
    """M08-M11: grafía, keyword, constante, f-string y duplicado derrumban el ancla."""
    inventario = _inventario_de_codigo(codigo, "sintetico.py")
    assert inventario == Counter({("sintetico.py", "f"): esperado}), f"{nombre}: {inventario}"


def test_el_ancla_no_confunde_lectura_comentario_ni_documentacion() -> None:
    """Fail-closed sin falsos positivos: sólo cuenta conversión ejecutable."""
    codigo = (
        '"""PRAGMA journal_mode=WAL mencionado en la docstring."""\n'
        "# PRAGMA journal_mode=WAL mencionado en un comentario\n"
        "async def f(conn, logger):\n"
        "    async with conn.execute('PRAGMA journal_mode') as cur:\n"
        "        await cur.fetchone()\n"
        "    await conn.execute('PRAGMA journal_mode=DELETE')\n"
        "    logger.info('PRAGMA journal_mode=WAL')\n"
    )
    assert _inventario_de_codigo(codigo, "sintetico.py") == Counter()


def test_el_ancla_cubre_scripts_y_local_scripts() -> None:
    """El alcance declarado no puede encogerse en silencio."""
    rutas = {_relativo(ruta) for ruta in _modulos_analizados()}
    assert any(ruta.startswith("sky_claw/") for ruta in rutas)
    assert any(ruta.startswith("scripts/") for ruta in rutas)
    assert any(ruta.startswith("local_scripts/") for ruta in rutas)


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
