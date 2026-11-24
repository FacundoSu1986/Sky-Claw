"""A1 — el journal de LOOT no afirma un rollback que no ocurrió.

Propiedad del mecanismo: *una transacción del sort sólo se marca ROLLED_BACK si
el filesystem prueba la recuperación* (``SnapshotTransactionLock.rollback_completed``
sin veto). Si el restore se VETÓ por terminalidad desconocida del worker
(``rollback_veto.exception_forbids_rollback``: LOOT pudo seguir vivo y restaurar
pisaría su escritura) o quedó INCOMPLETO (un snapshot no se pudo reinstalar), la
TX queda PENDIENTE, se emite un ``critical`` con ``tx_id`` y la respuesta dice
dónde está el snapshot para la recuperación manual.

Es la regla que ya cumplían los hermanos: ``synthesis_service._evaluar_recuperacion_rollback``
y ``dyndolod_service`` (TX PENDIENTE + critical). LOOT marcaba ROLLED_BACK en
TODAS sus ramas de excepción.

El ancla AST enumera las ramas ``except`` de ``sort_load_order``: una rama nueva
que cierre la TX por su cuenta rompe el test.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.db.journal import TransactionStatus
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.rollback_veto import mark_unknown_terminality
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.cli import (
    LOOTNotFoundError,
    LOOTPreconditionError,
    LOOTTimeoutError,
    LOOTWorkerProtocolError,
)
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.tools import loot_service as loot_service_module
from sky_claw.local.tools.loot_service import LootSortingService
from tests._loot_witness import TESTIGO_INTACTO

_PLUGINS = b"*Mod A.esp\n*Mod B.esp\n"
_MUTADO = b"*Mod B.esp\n*Mod A.esp\n"


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=5.0, max_retries=2, backoff_base=0.05)
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=snapshot_dir)
    await mgr.initialize()
    return mgr


@pytest.fixture
async def journal(tmp_path: pathlib.Path):  # noqa: ANN201
    from sky_claw.app.db.journal import OperationJournal

    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j
    await j.close()


def _plugins(tmp_path: pathlib.Path) -> tuple[LoadOrderFileResolver, pathlib.Path]:
    directorio = tmp_path / "load_order"
    directorio.mkdir()
    plugins = directorio / "plugins.txt"
    plugins.write_bytes(_PLUGINS)
    return LoadOrderFileResolver(explicit_dir=directorio), plugins


def _servicio(lock_manager: Any, snapshot_manager: Any, runner: Any, resolver: Any, journal: Any) -> LootSortingService:
    reporte = MagicMock(blocks_mutations=False)
    return LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=runner,
        load_order_resolver=resolver,
        preflight=MagicMock(run=AsyncMock(return_value=reporte), loot_version=None),
        journal=journal,
    )


def _runner_que_muta_y_lanza(plugins: pathlib.Path, excepcion: BaseException) -> MagicMock:
    async def sort(**_kwargs: object) -> LOOTResult:
        plugins.write_bytes(_MUTADO)
        raise excepcion

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort)
    return runner


async def _ultima_tx(journal: Any) -> Any:
    (ultima,) = await journal.list_recent_transactions(limit=1)
    return ultima


def _vetada(excepcion: BaseException) -> BaseException:
    mark_unknown_terminality(excepcion)
    return excepcion


# Toda excepción que el runner puede lanzar y que el servicio convierte en respuesta.
_EXCEPCIONES_DEL_RUNNER: list[tuple[str, Any]] = [
    ("timeout", lambda: LOOTTimeoutError(120)),
    ("precondicion", lambda: LOOTPreconditionError("x")),
    ("no_encontrado", lambda: LOOTNotFoundError("x")),
    ("protocolo", lambda: LOOTWorkerProtocolError("x")),
    ("inesperada", lambda: RuntimeError("x")),
]


@pytest.mark.parametrize(("nombre", "fabrica"), _EXCEPCIONES_DEL_RUNNER, ids=[n for n, _ in _EXCEPCIONES_DEL_RUNNER])
async def test_rollback_vetado_deja_la_tx_pendiente_y_no_restaura(
    lock_manager, snapshot_manager, journal, tmp_path, nombre: str, fabrica: Any
) -> None:  # noqa: ANN001
    resolver, plugins = _plugins(tmp_path)
    runner = _runner_que_muta_y_lanza(plugins, _vetada(fabrica()))
    res = await _servicio(lock_manager, snapshot_manager, runner, resolver, journal).sort_load_order()

    assert res["success"] is False, nombre
    assert res["rolled_back"] is False
    assert res["rollback_vetoed"] is True
    assert res["snapshots"], "la respuesta debe decir dónde quedó el snapshot"
    assert all(pathlib.Path(s["snapshot_path"]).is_file() for s in res["snapshots"])
    assert "vfs-quarantine" in res["outcome_detail"]
    # El veto existe para no pisar a un LOOT vivo: el archivo queda como quedó.
    assert plugins.read_bytes() == _MUTADO
    assert (await _ultima_tx(journal)).status == TransactionStatus.PENDING


async def test_cancelacion_con_rollback_vetado_deja_la_tx_pendiente(
    lock_manager, snapshot_manager, journal, tmp_path
) -> None:  # noqa: ANN001
    resolver, plugins = _plugins(tmp_path)
    runner = _runner_que_muta_y_lanza(plugins, _vetada(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _servicio(lock_manager, snapshot_manager, runner, resolver, journal).sort_load_order()

    assert (await _ultima_tx(journal)).status == TransactionStatus.PENDING


async def test_cancelacion_con_rollback_completo_marca_rolled_back(
    lock_manager, snapshot_manager, journal, tmp_path
) -> None:  # noqa: ANN001
    resolver, plugins = _plugins(tmp_path)
    runner = _runner_que_muta_y_lanza(plugins, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _servicio(lock_manager, snapshot_manager, runner, resolver, journal).sort_load_order()

    assert plugins.read_bytes() == _PLUGINS
    assert (await _ultima_tx(journal)).status == TransactionStatus.ROLLED_BACK


@pytest.mark.parametrize(("nombre", "fabrica"), _EXCEPCIONES_DEL_RUNNER, ids=[n for n, _ in _EXCEPCIONES_DEL_RUNNER])
async def test_rollback_completo_marca_rolled_back(
    lock_manager, snapshot_manager, journal, tmp_path, nombre: str, fabrica: Any
) -> None:  # noqa: ANN001
    resolver, plugins = _plugins(tmp_path)
    runner = _runner_que_muta_y_lanza(plugins, fabrica())
    res = await _servicio(lock_manager, snapshot_manager, runner, resolver, journal).sort_load_order()

    assert res["rolled_back"] is True, nombre
    assert res["rollback_vetoed"] is False
    assert "snapshots" not in res
    assert plugins.read_bytes() == _PLUGINS
    assert (await _ultima_tx(journal)).status == TransactionStatus.ROLLED_BACK


async def test_restore_incompleto_deja_la_tx_pendiente_con_critical(
    lock_manager, snapshot_manager, journal, tmp_path, caplog
) -> None:  # noqa: ANN001
    """Veredicto FAIL (no atribuible) + un snapshot que no se puede reinstalar."""
    resolver, plugins = _plugins(tmp_path)

    async def sort(**_kwargs: object) -> LOOTResult:
        plugins.write_bytes(_MUTADO)
        return LOOTResult(return_code=0, execution_witness=TESTIGO_INTACTO)

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort)
    snapshot_manager.restore_snapshot = AsyncMock(side_effect=OSError("solo lectura"))  # type: ignore[method-assign]

    with caplog.at_level(logging.CRITICAL, logger=loot_service_module.logger.name):
        res = await _servicio(lock_manager, snapshot_manager, runner, resolver, journal).sort_load_order()

    tx = await _ultima_tx(journal)
    assert res["success"] is False
    assert res["rolled_back"] is False
    assert res["rollback_vetoed"] is False
    assert res["snapshots"]
    assert tx.status == TransactionStatus.PENDING
    criticos = [
        r for r in caplog.records if r.levelno == logging.CRITICAL and r.name == loot_service_module.logger.name
    ]
    assert criticos, "el rollback incompleto debe dejar un registro critical"
    assert all(getattr(r, "tx_id", None) == tx.transaction_id for r in criticos)


# =============================================================================
# Ancla: TODA rama except de sort_load_order cierra la TX por el helper único
# =============================================================================

#: Ramas ``except`` de ``sort_load_order`` (nombres de los tipos capturados), en orden.
_RAMAS = [
    "LockAcquisitionError",
    "_ActionManifestError",
    "_LootSortFailedError",
    "LOOTTimeoutError",
    "LOOTNotFoundError|LOOTPreconditionError",
    "LOOTWorkerProtocolError",
    "asyncio.CancelledError",
    "Exception",
]
#: La única rama sin TX del journal: el lock nunca se adquirió.
_RAMAS_SIN_TX = {"LockAcquisitionError"}


def _ramas_except() -> list[tuple[str, ast.ExceptHandler]]:
    """Ramas del ``try`` que envuelve ``async with tx`` (las que cierran la TX)."""
    arbol = ast.parse(pathlib.Path(loot_service_module.__file__).read_text(encoding="utf-8"))
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "sort_load_order")
    (bloque,) = [
        n
        for n in ast.walk(funcion)
        if isinstance(n, ast.Try) and any(isinstance(hijo, ast.AsyncWith) for hijo in n.body)
    ]
    ramas: list[tuple[str, ast.ExceptHandler]] = []
    for rama in bloque.handlers:
        assert rama.type is not None, "un except desnudo no es una rama admisible"
        tipos = rama.type.elts if isinstance(rama.type, ast.Tuple) else [rama.type]
        ramas.append(("|".join(ast.unparse(t) for t in tipos), rama))
    return ramas


def _llama_metodo(nodo: ast.AST, nombre: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == nombre for n in ast.walk(nodo)
    )


def test_las_ramas_except_de_sort_load_order_estan_enumeradas() -> None:
    assert [nombre for nombre, _ in _ramas_except()] == _RAMAS


def test_toda_rama_con_tx_cierra_por_el_helper_y_ninguna_marca_a_mano() -> None:
    for nombre, rama in _ramas_except():
        assert not _llama_metodo(rama, "_mark_journal_rolled_back"), nombre
        if nombre in _RAMAS_SIN_TX:
            continue
        assert _llama_metodo(rama, "_cerrar_journal_tras_fallo"), nombre


def test_aviso_si_el_rollback_no_se_intento_no_manda_a_restaurar_a_ciegas() -> None:
    """Revisión adversarial: con la lease perdida el lock NO intenta el restore (otro
    agente pudo escribir el load order). El aviso no puede ordenar restaurar a ciegas."""
    from sky_claw.local.tools.loot_service import _Recuperacion

    no_intentado = _Recuperacion(
        vetado=False, intentado=False, probada=False, snapshots=(("plugins.txt", "snap/plugins.txt"),)
    )
    incompleto = _Recuperacion(
        vetado=False, intentado=True, probada=False, snapshots=(("plugins.txt", "snap/plugins.txt"),)
    )

    assert "NO se ejecutó" in no_intentado.aviso()
    assert "verificá" in no_intentado.aviso().lower()
    assert "INCOMPLETO" in incompleto.aviso()
