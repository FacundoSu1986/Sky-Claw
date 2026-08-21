"""Contrato de éxito del sort de LOOT — auto-sort con output real.

Ancla el contrato investigado en la tarea ``fix-loot-success-contract``:
LOOT (GUI, verificado contra upstream ``loot/loot`` ``src/gui/qt/main.cpp`` +
``docs/app/usage/initialisation.rst``) ordena, aplica el load order y cierra
sin imprimir ninguna lista numerada por stdout/stderr. Una corrida real
exitosa llega al parser con ``sorted_plugins == []``, y eso NO debe
convertirse en fallo ni en rollback del sort válido.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.tools.loot_service import LootSortingService

if TYPE_CHECKING:
    import pathlib

_CONTENIDO_ORIGINAL = "Skyrim.esm\nOriginal.esp\n"


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
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


def _preparar_load_order(tmp_path: pathlib.Path) -> tuple[LoadOrderFileResolver, pathlib.Path]:
    """Crea un plugins.txt/loadorder.txt reales y un resolver apuntándoles."""
    load_order_dir = tmp_path / "load_order"
    load_order_dir.mkdir()
    plugins = load_order_dir / "plugins.txt"
    plugins.write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")
    (load_order_dir / "loadorder.txt").write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")
    return LoadOrderFileResolver(explicit_dir=load_order_dir), plugins


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    resolver: LoadOrderFileResolver,
) -> LootSortingService:
    """Preflight verde mockeado: este archivo ancla el contrato de éxito, no el
    semáforo (cubierto por test_preflight_service/test_preflight_wiring)."""
    reporte = MagicMock()
    reporte.blocks_mutations = False
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)
    preflight.loot_version = None
    return LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=runner,
        load_order_resolver=resolver,
        preflight=preflight,
    )


@pytest.mark.asyncio
async def test_sort_real_sin_lista_de_plugins_no_es_falso_fallo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """CASO A/E: LOOT real aplica el orden y NO imprime la lista de plugins.

    Verificado contra upstream (loot/loot ``src/gui/qt/main.cpp`` +
    ``docs/app/usage/initialisation.rst``): ``--auto-sort`` ordena, aplica y
    cierra; la GUI no emite ninguna lista numerada por stdout/stderr, así que
    una corrida real exitosa llega con ``sorted_plugins == []``. El contrato
    anterior (``success`` exige ``len(sorted_plugins) > 0``) convertía ese sort
    válido en fallo y REVERTÍA el archivo ya ordenado: este test ancla que el
    orden aplicado se conserva.
    """
    resolver, plugins = _preparar_load_order(tmp_path)
    orden_nuevo = "Skyrim.esm\nActualizado.esp\n"

    async def sort_real(**_kwargs: object) -> LOOTResult:
        # LOOT reescribió el load order (libloot set_load_order → save()) y
        # salió 0 sin imprimir nada.
        plugins.write_text(orden_nuevo, encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=[], errors=[])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_real)
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    result = await svc.sort_load_order()

    assert result["success"] is True
    assert result["rolled_back"] is False
    assert plugins.read_text(encoding="utf-8") == orden_nuevo


@pytest.mark.asyncio
async def test_fallo_con_output_vacio_lleva_mensaje_accionable(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Un exit non-zero sin texto parseable (LOOT GUI no imprime por consola)
    no puede dejar ``success=False`` con ``message`` vacío: el código de salida
    es la causa mínima identificable."""
    resolver, plugins = _preparar_load_order(tmp_path)

    async def sort_fallido(**_kwargs: object) -> LOOTResult:
        plugins.write_text("CORRUPTO\n", encoding="utf-8")
        return LOOTResult(return_code=7, sorted_plugins=[], errors=[])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_fallido)
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert result["message"] == "LOOT sort failed with exit code 7."
    assert result["rolled_back"] is True
    assert plugins.read_text(encoding="utf-8") == _CONTENIDO_ORIGINAL
