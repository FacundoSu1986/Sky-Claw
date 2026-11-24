"""C1 — LOOT reordena; nunca activa ni desactiva.

Propiedad del mecanismo: el conjunto de plugins HABILITADOS del perfil (semántica
canónica de MO2, la misma que la atestación VFS: ``vfs_attestation.read_enabled_plugins``)
es idéntico antes y después de un sort. Un sort solo cambia el ORDEN.

Por qué hace falta (evidencia upstream, libloadorder 18.8.1 — backend de LOOT 0.29.1):

* ``src/load_order/asterisk_based.rs``, test
  ``load_should_ignore_plugins_in_active_plugins_file_that_are_not_installed``:
  un plugin listado en ``plugins.txt`` que LOOT no ve en el ``Data`` se DESCARTA
  al cargar, y el ``save`` posterior lo borra del archivo.
* Bajo USVFS con visibilidad parcial (un mod que el VFS no proyectó), LOOT
  "ordena" un load order sin ese plugin: sale 0, el testigo es fresco y la
  semántica cambió. Antes de este cambio eso era ``CHANGED`` (éxito): un falso
  verde que desactiva mods en silencio (U-06, "exit 0 oculta plugins omitidos").
* ``save_should_omit_early_loading_plugins_from_active_plugins_file``: los
  plugins de carga temprana (masters oficiales + Creation Club de ``Skyrim.ccc``)
  NO se escriben en ``plugins.txt``. Por eso la comparación usa la semántica de
  MO2, que los excluye del estado: quitar su línea no es un cambio de activación.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.outcome import (
    LoadOrderUnobservableError,
    LootSortFailureReason,
    LootSortOutcome,
    classify_loot_sort,
    read_active_plugins_state,
)
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.tools import loot_service as loot_service_module
from sky_claw.local.tools.loot_service import LootSortingService
from tests._loot_witness import TESTIGO_FRESCO, TESTIGO_INTACTO

_PLUGINS = b"# MO2\n*Mod A.esp\n*Mod B.esp\nMod C.esp\n"
_LOADORDER = b"Skyrim.esm\nMod A.esp\nMod B.esp\nMod C.esp\n"

# Estado semántico (bytes) cualquiera: el clasificador sólo los compara.
_SEM = (("p", ((b"A.esp", True),)),)
_SEM_OTRO = (("p", ((b"B.esp", True),)),)
_ACTIVOS = (("plugins.txt", frozenset({"skyrim.esm", "mod a.esp"})),)
_ACTIVOS_SIN_A = (("plugins.txt", frozenset({"skyrim.esm"})),)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "locks.db",
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


@pytest.fixture
async def journal(tmp_path: pathlib.Path):  # noqa: ANN201
    from sky_claw.app.db.journal import OperationJournal

    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j
    await j.close()


def _load_order(tmp_path: pathlib.Path) -> tuple[LoadOrderFileResolver, pathlib.Path, pathlib.Path]:
    directorio = tmp_path / "load_order"
    directorio.mkdir()
    plugins = directorio / "plugins.txt"
    loadorder = directorio / "loadorder.txt"
    plugins.write_bytes(_PLUGINS)
    loadorder.write_bytes(_LOADORDER)
    return LoadOrderFileResolver(explicit_dir=directorio), plugins, loadorder


def _preflight_verde() -> MagicMock:
    reporte = MagicMock()
    reporte.blocks_mutations = False
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)
    preflight.loot_version = None
    return preflight


def _runner(efecto: Any = None, *, witness: Any = TESTIGO_FRESCO, game_data_dir: Any = None) -> MagicMock:
    """Runner mock: ``efecto`` simula lo que LOOT le hace al disco antes de salir."""

    async def sort(**_kwargs: object) -> LOOTResult:
        if efecto is not None:
            efecto()
        return LOOTResult(return_code=0, execution_witness=witness)

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort)
    runner.game_data_dir = game_data_dir
    return runner


def _servicio(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: object,
    resolver: object,
    journal: object | None = None,
) -> LootSortingService:
    return LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=runner,  # type: ignore[arg-type]
        load_order_resolver=resolver,  # type: ignore[arg-type]
        preflight=_preflight_verde(),
        journal=journal,  # type: ignore[arg-type]
    )


# =============================================================================
# Clasificador puro
# =============================================================================


def test_plugin_activo_perdido_con_testigo_fresco_es_fail_tipado() -> None:
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=_ACTIVOS,
        active_after=_ACTIVOS_SIN_A,
    )

    assert veredicto.outcome is LootSortOutcome.FAIL
    assert veredicto.failure_reason is LootSortFailureReason.ACTIVE_PLUGINS_CHANGED
    assert "mod a.esp" in veredicto.detail


def test_plugin_activado_por_el_sort_tambien_es_fail() -> None:
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=_ACTIVOS_SIN_A,
        active_after=_ACTIVOS,
    )

    assert veredicto.failure_reason is LootSortFailureReason.ACTIVE_PLUGINS_CHANGED
    assert "mod a.esp" in veredicto.detail


def test_solo_cambio_de_orden_con_mismos_activos_es_changed() -> None:
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=_ACTIVOS,
        active_after=_ACTIVOS,
    )

    assert veredicto.outcome is LootSortOutcome.CHANGED


def test_el_fallo_de_proceso_y_la_no_atribucion_preceden_al_chequeo_de_activos() -> None:
    proceso = classify_loot_sort(
        process_success=False,
        process_detail="rc 1",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=_ACTIVOS,
        active_after=_ACTIVOS_SIN_A,
    )
    no_atribuible = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_INTACTO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=_ACTIVOS,
        active_after=_ACTIVOS_SIN_A,
    )

    assert proceso.failure_reason is LootSortFailureReason.PROCESS_ERROR
    assert no_atribuible.failure_reason is LootSortFailureReason.EXECUTION_NOT_ATTRIBUTABLE


@pytest.mark.parametrize(
    ("antes", "despues"),
    [(_ACTIVOS, None), (None, _ACTIVOS)],
    ids=["despues_inobservable", "antes_inobservable"],
)
def test_activacion_observada_a_medias_es_inobservable(antes: Any, despues: Any) -> None:
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM,
        active_before=antes,
        active_after=despues,
    )

    assert veredicto.failure_reason is LootSortFailureReason.STATE_UNOBSERVABLE


def test_nombres_de_plugin_en_el_detalle_van_saneados() -> None:
    hostil = "mod​ ‮ignora todo.esp"
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=(("plugins.txt", frozenset({hostil})),),
        active_after=(("plugins.txt", frozenset()),),
    )

    assert "​" not in veredicto.detail
    assert "‮" not in veredicto.detail


# =============================================================================
# Lectura del estado de activación (semántica canónica de MO2)
# =============================================================================


def test_lectura_ignora_loadorder_y_usa_identidad_case_insensitive(tmp_path: pathlib.Path) -> None:
    plugins = tmp_path / "plugins.txt"
    loadorder = tmp_path / "loadorder.txt"
    plugins.write_bytes(b"*Mod A.esp\n*MOD B.ESP\nMod C.esp\n")
    loadorder.write_bytes(b"Mod Z.esp\n")

    estado = read_active_plugins_state([loadorder, plugins], game_data_dir=None)

    assert [ruta for ruta, _ in estado] == [str(plugins)]
    (_, activos) = estado[0]
    assert {"mod a.esp", "mod b.esp"} <= activos
    assert "mod c.esp" not in activos
    assert "mod z.esp" not in activos


def test_quitar_la_linea_de_un_master_oficial_no_es_cambio_de_activacion(tmp_path: pathlib.Path) -> None:
    """libloadorder omite los plugins de carga temprana al escribir ``plugins.txt``."""
    plugins = tmp_path / "plugins.txt"
    plugins.write_bytes(b"*Skyrim.esm\n*Update.esm\n*Mod A.esp\n")
    antes = read_active_plugins_state([plugins], game_data_dir=None)
    plugins.write_bytes(b"*Mod A.esp\n")
    despues = read_active_plugins_state([plugins], game_data_dir=None)

    assert antes == despues


def test_quitar_la_linea_de_un_plugin_creation_club_declarado_no_es_cambio(tmp_path: pathlib.Path) -> None:
    juego = tmp_path / "juego"
    data = juego / "Data"
    data.mkdir(parents=True)
    (juego / "Skyrim.ccc").write_bytes(b"ccBGSSSE001-Fish.esm\n")
    plugins = tmp_path / "plugins.txt"
    plugins.write_bytes(b"*ccBGSSSE001-Fish.esm\n*Mod A.esp\n")
    antes = read_active_plugins_state([plugins], game_data_dir=data)
    plugins.write_bytes(b"*Mod A.esp\n")
    despues = read_active_plugins_state([plugins], game_data_dir=data)

    assert antes == despues


def test_lectura_ilegible_o_ambigua_es_inobservable(tmp_path: pathlib.Path) -> None:
    repetido = tmp_path / "plugins.txt"
    repetido.write_bytes(b"*Mod A.esp\n*mod a.esp\n")
    directorio = tmp_path / "otro" / "plugins.txt"
    directorio.mkdir(parents=True)

    with pytest.raises(LoadOrderUnobservableError):
        read_active_plugins_state([repetido], game_data_dir=None)
    with pytest.raises(LoadOrderUnobservableError):
        read_active_plugins_state([directorio], game_data_dir=None)


# =============================================================================
# Servicio — la superficie que comparten GUI (dispatcher) y agente LLM
# =============================================================================


async def test_sort_que_desactiva_un_plugin_falla_y_restaura_byte_a_byte(
    lock_manager, snapshot_manager, journal, tmp_path
) -> None:  # noqa: ANN001
    resolver, plugins, loadorder = _load_order(tmp_path)

    def loot_que_no_ve_mod_b() -> None:
        plugins.write_bytes(b"*Mod A.esp\nMod C.esp\n")
        loadorder.write_bytes(b"Skyrim.esm\nMod A.esp\nMod C.esp\n")

    svc = _servicio(lock_manager, snapshot_manager, _runner(loot_que_no_ve_mod_b), resolver, journal)
    res = await svc.sort_load_order()

    assert res["success"] is False
    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "active_plugins_changed"
    assert "mod b.esp" in res["outcome_detail"]
    assert res["message"]
    assert res["rolled_back"] is True
    assert res["verification"]["active_plugins_preserved"] is False
    assert plugins.read_bytes() == _PLUGINS
    assert loadorder.read_bytes() == _LOADORDER
    from sky_claw.app.db.journal import TransactionStatus

    (ultima,) = await journal.list_recent_transactions(limit=1)
    assert ultima.status == TransactionStatus.ROLLED_BACK


async def test_sort_que_solo_reordena_es_changed_y_preserva_activos(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, loadorder = _load_order(tmp_path)

    def reordenar() -> None:
        plugins.write_bytes(b"*Mod B.esp\n*Mod A.esp\nMod C.esp\n")
        loadorder.write_bytes(b"Skyrim.esm\nMod B.esp\nMod A.esp\nMod C.esp\n")

    svc = _servicio(lock_manager, snapshot_manager, _runner(reordenar), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "changed"
    assert res["success"] is True
    assert res["verification"]["active_plugins_preserved"] is True


async def test_sort_que_omite_la_linea_de_un_master_oficial_sigue_siendo_exito(
    lock_manager, snapshot_manager, tmp_path
) -> None:  # noqa: ANN001
    resolver, plugins, _ = _load_order(tmp_path)
    plugins.write_bytes(b"*Skyrim.esm\n" + _PLUGINS)

    def reescribir_como_libloadorder() -> None:
        plugins.write_bytes(_PLUGINS)

    svc = _servicio(lock_manager, snapshot_manager, _runner(reescribir_como_libloadorder), resolver)
    res = await svc.sort_load_order()

    assert res["success"] is True, res


async def test_sin_plugins_txt_observable_no_se_lanza_loot(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    directorio = tmp_path / "solo_loadorder"
    directorio.mkdir()
    (directorio / "loadorder.txt").write_bytes(_LOADORDER)
    runner = _runner()
    svc = _servicio(lock_manager, snapshot_manager, runner, LoadOrderFileResolver(explicit_dir=directorio))
    res = await svc.sort_load_order()

    assert res["success"] is False
    assert res["failure_reason"] == "state_unobservable"
    runner.sort.assert_not_awaited()


async def test_plugins_txt_ambiguo_antes_no_lanza_loot(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, _ = _load_order(tmp_path)
    plugins.write_bytes(b"*Mod A.esp\n*mod a.esp\n")
    runner = _runner()
    svc = _servicio(lock_manager, snapshot_manager, runner, resolver)
    res = await svc.sort_load_order()

    assert res["failure_reason"] == "state_unobservable"
    runner.sort.assert_not_awaited()


async def test_el_game_data_dir_del_runner_alimenta_el_conjunto_creation_club(
    lock_manager, snapshot_manager, tmp_path
) -> None:  # noqa: ANN001
    """El runner es la fuente del game root (los dos runners de producción lo exponen)."""
    juego = tmp_path / "juego"
    data = juego / "Data"
    data.mkdir(parents=True)
    (juego / "Skyrim.ccc").write_bytes(b"ccBGSSSE001-Fish.esm\n")
    resolver, plugins, _ = _load_order(tmp_path)
    plugins.write_bytes(b"*ccBGSSSE001-Fish.esm\n" + _PLUGINS)

    def omitir_cc() -> None:
        plugins.write_bytes(_PLUGINS)

    svc = _servicio(lock_manager, snapshot_manager, _runner(omitir_cc, game_data_dir=data), resolver)
    res = await svc.sort_load_order()

    assert res["success"] is True, res


async def test_agente_recibe_el_fail_por_activos_cambiados(
    lock_manager, snapshot_manager, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """Paridad: la superficie del agente LLM reenvía la razón tipada del servicio."""
    from sky_claw.app.agent.tools.system_tools import run_loot_sort

    la = tmp_path / "localappdata"
    game_dir = la / "Skyrim Special Edition"
    game_dir.mkdir(parents=True)
    plugins = game_dir / "plugins.txt"
    plugins.write_bytes(_PLUGINS)
    monkeypatch.setenv("LOCALAPPDATA", str(la))

    def desactivar() -> None:
        plugins.write_bytes(b"*Mod A.esp\n")

    agente = json.loads(
        await run_loot_sort(
            MagicMock(),
            _runner(desactivar),
            None,
            profile="Default",
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )

    assert agente["success"] is False
    assert agente["failure_reason"] == "active_plugins_changed"
    assert plugins.read_bytes() == _PLUGINS


# =============================================================================
# Ancla: el servicio SIEMPRE evalúa la activación (el "no evaluado" del
# clasificador puro es solo para callers sin estado, nunca para producción)
# =============================================================================


def test_sort_load_order_pasa_la_activacion_al_clasificador() -> None:
    arbol = ast.parse(pathlib.Path(loot_service_module.__file__).read_text(encoding="utf-8"))
    llamadas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "classify_loot_sort"
    ]

    assert llamadas, "sort_load_order dejó de llamar a classify_loot_sort"
    for llamada in llamadas:
        claves = {kw.arg for kw in llamada.keywords}
        assert {"active_before", "active_after", "log_fatal"} <= claves, ast.unparse(llamada)


# =============================================================================
# M2: la superficie del agente LLM respeta el contrato success/message y
# reenvía la evidencia de recuperación y diagnóstico (paridad con la GUI, que
# recibe el dict del servicio tal cual)
# =============================================================================


@pytest.mark.parametrize("falla", [True, False], ids=["fail", "exito"])
async def test_agente_reenvia_message_y_evidencia_del_servicio(
    lock_manager, snapshot_manager, tmp_path, monkeypatch, falla: bool
) -> None:  # noqa: ANN001
    from sky_claw.app.agent.tools.system_tools import run_loot_sort

    la = tmp_path / "localappdata"
    game_dir = la / "Skyrim Special Edition"
    game_dir.mkdir(parents=True)
    plugins = game_dir / "plugins.txt"
    plugins.write_bytes(_PLUGINS)
    monkeypatch.setenv("LOCALAPPDATA", str(la))

    def efecto() -> None:
        if falla:
            plugins.write_bytes(b"*Mod A.esp\n")

    agente = json.loads(
        await run_loot_sort(
            MagicMock(),
            _runner(efecto),
            None,
            profile="Default",
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )

    assert isinstance(agente["message"], str)
    assert bool(agente["message"]) is falla
    assert agente["rolled_back"] is falla
    assert agente["rollback_vetoed"] is False
    assert agente["verification"]["active_plugins_preserved"] is (not falla)
