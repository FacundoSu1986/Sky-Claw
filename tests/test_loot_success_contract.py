"""Contrato de éxito del sort de LOOT — auto-sort con output real.

Ancla el contrato investigado en la tarea ``fix-loot-success-contract``:
LOOT (GUI, verificado contra upstream ``loot/loot`` ``src/gui/qt/main.cpp`` +
``docs/app/usage/initialisation.rst``) ordena, aplica el load order y cierra
sin imprimir ninguna lista numerada por stdout/stderr. Una corrida real
exitosa llega al parser con ``sorted_plugins == []``, y eso NO debe
convertirse en fallo ni en rollback del sort válido.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver, LoadOrderPaths
from sky_claw.local.tools import loot_service as loot_service_module
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
    # Contrato canónico de tools: message vacío en éxito.
    assert result["message"] == ""
    assert result["rolled_back"] is False
    assert plugins.read_text(encoding="utf-8") == orden_nuevo


@pytest.mark.asyncio
async def test_rc0_sin_mutacion_no_se_reporta_como_exito(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """CASO mutex (loot/loot ``src/gui/qt/main.cpp``): con otra instancia de
    LOOT ya abierta, el proceso nuevo sale 0 enfocando la ventana existente
    SIN sortear. rc=0 sin mutación observable de los targets es incertidumbre:
    se falla con mensaje accionable y el snapshot se restaura (no-op aquí,
    pero el estado reportado es verdadero).
    """
    resolver, plugins = _preparar_load_order(tmp_path)
    runner = MagicMock()
    runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=[], errors=[]))
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "ningún archivo del load order cambió" in result["message"]
    assert plugins.read_text(encoding="utf-8") == _CONTENIDO_ORIGINAL


@pytest.mark.asyncio
async def test_no_op_idempotente_con_reescritura_es_exito(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """CASO B (idempotencia): el orden ya era el correcto y LOOT igual reescribe
    los archivos al aplicar (libloot ``set_load_order`` → ``save()``
    incondicional). La reescritura ES la evidencia física: éxito sin rollback
    aunque el contenido no cambie.
    """
    resolver, plugins = _preparar_load_order(tmp_path)

    async def sort_idempotente(**_kwargs: object) -> LOOTResult:
        for archivo in (plugins, plugins.with_name("loadorder.txt")):
            archivo.write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=[], errors=[])

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_idempotente)
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    result = await svc.sort_load_order()

    assert result["success"] is True
    assert result["rolled_back"] is False
    assert plugins.read_text(encoding="utf-8") == _CONTENIDO_ORIGINAL


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


@pytest.mark.asyncio
async def test_fallo_con_output_solo_whitespace_lleva_mensaje_accionable(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Un stderr/stdout solo-whitespace no cuenta como mensaje: el fallback del
    exit code sigue siendo el diagnóstico mínimo (review adversarial #495)."""
    resolver, plugins = _preparar_load_order(tmp_path)

    async def sort_fallido(**_kwargs: object) -> LOOTResult:
        plugins.write_text("CORRUPTO\n", encoding="utf-8")
        return LOOTResult(return_code=7, sorted_plugins=[], errors=[], raw_stderr="\n  \n", raw_stdout="")

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort_fallido)
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert result["message"] == "LOOT sort failed with exit code 7."


@pytest.mark.asyncio
async def test_rc0_sin_targets_observables_falla_cerrado(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Sin archivos de load order observables no hay evidencia que atribuir:
    rc=0 (incluido el mutex early-exit de main.cpp) NO se reporta como éxito
    (review adversarial #495 — incertidumbre → fallo cerrado)."""
    resolver = MagicMock()
    resolver.resolve.return_value = LoadOrderPaths(files=(), sources=())
    runner = MagicMock()
    runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=[], errors=[]))
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    result = await svc.sort_load_order()

    assert result["success"] is False
    assert "No hay archivos de load order observables" in result["message"]
    assert result["rolled_back"] is False  # nada que restaurar: snapshot vacío


@pytest.mark.asyncio
async def test_stat_ilegible_en_post_falla_cerrado(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Si el estado FINAL no se puede inspeccionar (stat falla por permisos/IO),
    no se deduce "cambió" ni "no cambió": fail-closed con detalle (review
    adversarial #495 — UNREADABLE ≠ ABSENT ≠ changed)."""
    resolver, plugins = _preparar_load_order(tmp_path)
    runner = MagicMock()
    runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=[], errors=[]))
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    real_capture = loot_service_module._capturar_estado_de_un_archivo
    llamadas = {"n": 0}

    def captura_spy(path: pathlib.Path):
        llamadas["n"] += 1
        # PRE captura los 2 targets (calls 1-2, legibles); POST los evalúa
        # (calls 3-4): simular ilegible solo en la re-evaluación.
        if llamadas["n"] >= 3:
            return loot_service_module._EstadoDeArchivo(tipo="ilegible")
        return real_capture(path)

    with patch.object(loot_service_module, "_capturar_estado_de_un_archivo", captura_spy):
        result = await svc.sort_load_order()

    assert result["success"] is False
    assert "No se pudo inspeccionar el estado del load order" in result["message"]
    assert "tras la corrida" in result["message"]


@pytest.mark.asyncio
async def test_stat_ilegible_en_pre_falla_cerrado(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Si la evidencia BASE (pre-sort) no se pudo capturar para un target, la
    comparación posterior no es verificable: fail-closed (review adversarial
    #495 — PRE UNREADABLE → FAIL CLOSED)."""
    resolver, plugins = _preparar_load_order(tmp_path)
    runner = MagicMock()
    runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=[], errors=[]))
    svc = _make_service(lock_manager, snapshot_manager, runner, resolver)

    real_capture = loot_service_module._capturar_estado_de_un_archivo
    llamadas = {"n": 0}

    def captura_spy(path: pathlib.Path):
        llamadas["n"] += 1
        # El primer target capturado en PRE es ilegible: la baseline queda
        # incompleta y la evaluación debe fallar cerrada.
        if llamadas["n"] == 1:
            return loot_service_module._EstadoDeArchivo(tipo="ilegible")
        return real_capture(path)

    with patch.object(loot_service_module, "_capturar_estado_de_un_archivo", captura_spy):
        result = await svc.sort_load_order()

    assert result["success"] is False
    assert "No se pudo inspeccionar el estado del load order" in result["message"]


def test_tri_estado_distingue_ausente_de_ilegible(tmp_path: pathlib.Path) -> None:
    """Unit: FileNotFoundError → ausente; PermissionError → ilegible; stat OK →
    presente. Nunca se confunden (review adversarial #495 P1)."""
    f = tmp_path / "plugins.txt"

    assert loot_service_module._capturar_estado_de_un_archivo(f).tipo == "ausente"

    with patch.object(pathlib.Path, "stat", side_effect=PermissionError("denied")):
        assert loot_service_module._capturar_estado_de_un_archivo(f).tipo == "ilegible"

    f.write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")
    presente = loot_service_module._capturar_estado_de_un_archivo(f)
    assert presente.tipo == "presente"
    # size = bytes REALES en disco (write_text traduce a \r\n en Windows).
    assert presente.size == len(f.read_bytes())


def test_evaluar_evidencia_ilegible_nunca_cuenta_como_cambio(tmp_path: pathlib.Path) -> None:
    """Unit: pre/posta ilegible → (False, path); pre ausente + post presente →
    cambio; pre presente + post igual → sin cambio."""
    from sky_claw.local.tools.loot_service import _EstadoDeArchivo, _evaluar_evidencia

    f = tmp_path / "loadorder.txt"
    f.write_text(_CONTENIDO_ORIGINAL, encoding="utf-8")

    # Ilegible en el post: no verificable → (False, path).
    with patch.object(pathlib.Path, "stat", side_effect=PermissionError("denied")):
        assert _evaluar_evidencia({f: _EstadoDeArchivo(tipo="presente", mtime_ns=1, size=1)}, [f]) == (
            False,
            f,
        )

    # Ilegible en el pre: no verificable → (False, path).
    estado_actual = loot_service_module._capturar_estado_de_un_archivo(f)
    assert _evaluar_evidencia({f: _EstadoDeArchivo(tipo="ilegible")}, [f]) == (False, f)

    # Pre ausente + post presente: creación observada → cambio.
    assert _evaluar_evidencia({f: _EstadoDeArchivo(tipo="ausente")}, [f]) == (True, None)

    # Pre y post idénticos: sin cambio.
    assert _evaluar_evidencia({f: estado_actual}, [f]) == (False, None)
