"""PR-2 — contrato de resultado verificable del sort de LOOT: CHANGED / NO_CHANGE / FAIL.

Congela la separación entre los dos estados que el gate previo mezclaba:

A) LOOT arrancó, ordenó y el orden ya era correcto → NO_CHANGE legítimo. En
   loot/loot 0.29.1 no hay apply ni ``SetLoadOrder`` si el orden no cambió
   (``src/gui/qt/main_window.cpp:1596-1617,2870-2884``; ``game.cpp:788-791``):
   ningún archivo se reescribe.
B) Otra instancia posee el mutex ``LOOT.Shell.Instance`` y el proceso nuevo sale 0
   antes de inicializar nada (``src/gui/qt/main.cpp:90-95``) → FAIL no atribuible.

Lo que los distingue es el testigo de ejecución (``LOOTDebugLog.txt`` recreado por
el constructor de ``LootState``, ``loot_state.cpp:105-106``), capturado por
``LOOTRunner`` alrededor del proceso y transportado como dato hasta el servicio.
Estos tests NO sustituyen el rig real (R1-R5): ningún mock prueba LOOT.exe.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import itertools
import json
import os
import pathlib
import tomllib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sky_claw.local.loot.execution_witness as witness_module
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.binary_version import LootBinaryVersionError, read_loot_binary_version
from sky_claw.local.loot.cli import (
    LOOT_FAILURE_KINDS,
    LOOTConfig,
    LOOTNotFoundError,
    LOOTPreconditionError,
    LOOTRunner,
    LOOTTimeoutError,
    LOOTWorkerProtocolError,
)
from sky_claw.local.loot.execution_witness import (
    LOOT_DEBUG_LOG_FILENAME,
    LootExecutionWitness,
    LootExecutionWitnessPayloadError,
    LootExecutionWitnessState,
    arm_execution_witness,
)
from sky_claw.local.loot.headless_settings import (
    LOOT_LAST_VERSION_KEY,
    LOOT_NO_SORTING_CHANGES_DIALOG_KEY,
    LOOT_UPDATE_CHECK_KEY,
    MANAGED_LOOT_HEADLESS_SETTINGS,
    MANAGED_LOOT_SETTINGS_KEYS,
    LootHeadlessSettingsAction,
    LootHeadlessSettingsError,
    ensure_loot_headless_settings,
    managed_loot_settings,
)
from sky_claw.local.loot.outcome import (
    LootSortFailureReason,
    LootSortOutcome,
    classify_loot_sort,
    parse_load_order_bytes,
    read_load_order_semantics,
)
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver, LoadOrderPaths
from sky_claw.local.tools import loot_service as loot_service_module
from sky_claw.local.tools.loot_service import LootSortingService
from tests._loot_pe import escribir_loot_exe, pe_con_version
from tests._loot_witness import TESTIGO_FRESCO, TESTIGO_INTACTO

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

_PLUGINS_ORIGINAL = b"# MO2 generated\n*Skyrim.esm\n*Mod A.esp\nMod B.esp\n"
_LOADORDER_ORIGINAL = b"Skyrim.esm\nMod A.esp\nMod B.esp\n"

#: Versión del LOOT.exe falso por defecto (``tests/_loot_pe.py``).
_VERSION = "0.29.1"

#: Contenido EXACTO de un data root nuevo para un LOOT.exe 0.29.1.
_GESTIONADO = 'useNoSortingChangesDialog = false\nenableLootUpdateCheck = false\nlastVersion = "0.29.1"\n'


# =============================================================================
# Fixtures y helpers
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
    plugins.write_bytes(_PLUGINS_ORIGINAL)
    loadorder.write_bytes(_LOADORDER_ORIGINAL)
    return LoadOrderFileResolver(explicit_dir=directorio), plugins, loadorder


def _preflight_verde() -> MagicMock:
    reporte = MagicMock()
    reporte.blocks_mutations = False
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)
    preflight.loot_version = None
    return preflight


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


def _runner(efecto: Any = None, *, resultado: LOOTResult | None = None) -> MagicMock:
    """Runner mock: ``efecto`` simula lo que LOOT le hace al disco antes de salir."""

    async def sort(**_kwargs: object) -> LOOTResult:
        if efecto is not None:
            efecto()
        assert resultado is not None
        return resultado

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort)
    return runner


def _ok(witness: LootExecutionWitness | None = TESTIGO_FRESCO, **kwargs: Any) -> LOOTResult:
    kwargs.setdefault("return_code", 0)
    return LOOTResult(execution_witness=witness, **kwargs)


# =============================================================================
# Clasificador puro — matriz canónica enumerada (no muestreada)
# =============================================================================

_ESTADO = (("p", ((b"Skyrim.esm", True),)),)
_ESTADO_OTRO = (("p", ((b"Skyrim.esm", False),)),)
_TESTIGOS: tuple[LootExecutionWitness | None, ...] = (None,) + tuple(
    LootExecutionWitness(state=s) for s in LootExecutionWitnessState
)


def test_matriz_canonica_enumerada_sobre_todo_el_espacio() -> None:
    """Enumera TODO el producto (proceso × testigo × antes × después) y exige
    las dos propiedades del contrato, no una muestra de casos:

    * éxito ⇔ proceso ok ∧ testigo FRESH ∧ antes observable ∧ después observable
    * CHANGED ⇔ éxito ∧ antes != después ; NO_CHANGE ⇔ éxito ∧ antes == después
    """
    antes_opciones = (_ESTADO, None)
    despues_opciones = (_ESTADO, _ESTADO_OTRO, None)
    for proceso_ok, testigo, antes, despues in itertools.product(
        (True, False), _TESTIGOS, antes_opciones, despues_opciones
    ):
        veredicto = classify_loot_sort(
            process_success=proceso_ok,
            process_detail="rc 1",
            witness=testigo,
            before=antes,
            after=despues,
        )
        esperado_exito = (
            proceso_ok and testigo is not None and testigo.fresh and antes is not None and despues is not None
        )
        caso = (proceso_ok, testigo, antes, despues)
        assert veredicto.success is esperado_exito, caso
        if esperado_exito:
            esperado = LootSortOutcome.CHANGED if antes != despues else LootSortOutcome.NO_CHANGE
            assert veredicto.outcome is esperado, caso
            assert veredicto.failure_reason is None, caso
        else:
            assert veredicto.outcome is LootSortOutcome.FAIL, caso
            assert veredicto.failure_reason is not None, caso


@pytest.mark.parametrize(
    ("caso", "proceso_ok", "testigo", "antes", "despues", "outcome", "razon"),
    [
        ("A", True, TESTIGO_FRESCO, _ESTADO, _ESTADO_OTRO, LootSortOutcome.CHANGED, None),
        ("B", True, TESTIGO_FRESCO, _ESTADO, _ESTADO, LootSortOutcome.NO_CHANGE, None),
        ("C", False, TESTIGO_FRESCO, _ESTADO, _ESTADO, LootSortOutcome.FAIL, LootSortFailureReason.PROCESS_ERROR),
        ("D", True, TESTIGO_INTACTO, _ESTADO, _ESTADO, LootSortOutcome.FAIL, "execution_not_attributable"),
        ("E", True, TESTIGO_FRESCO, _ESTADO, None, LootSortOutcome.FAIL, LootSortFailureReason.STATE_UNOBSERVABLE),
        ("F", True, None, _ESTADO, _ESTADO_OTRO, LootSortOutcome.FAIL, "execution_not_attributable"),
        # Precedencia C → E → D/F: el fallo de proceso manda sobre todo lo demás…
        ("C>E", False, None, _ESTADO, None, LootSortOutcome.FAIL, LootSortFailureReason.PROCESS_ERROR),
        # …y sin estado final no se distingue un no-op de una mutación ajena.
        ("E>D", True, TESTIGO_INTACTO, _ESTADO, None, LootSortOutcome.FAIL, "state_unobservable"),
    ],
)
def test_matriz_canonica_casos_nombrados(
    caso: str,
    proceso_ok: bool,
    testigo: LootExecutionWitness | None,
    antes: Any,
    despues: Any,
    outcome: LootSortOutcome,
    razon: object,
) -> None:
    veredicto = classify_loot_sort(
        process_success=proceso_ok,
        process_detail="LOOT sort failed with exit code 1.",
        witness=testigo,
        before=antes,
        after=despues,
    )
    assert veredicto.outcome is outcome, caso
    assert veredicto.failure_reason == razon, caso


def test_valores_enumerables_congelados() -> None:
    """Igualdad literal: agregar un estado o una razón es una decisión de contrato."""
    assert {o.value for o in LootSortOutcome} == {"changed", "no_change", "fail"}
    assert {r.value for r in LootSortFailureReason} == {
        "precondition_failed",
        "process_error",
        "timeout",
        "execution_not_attributable",
        "state_unobservable",
        "protocol_error",
    }
    assert {s.value for s in LootExecutionWitnessState} == {
        "fresh",
        "sentinel_intact",
        "sentinel_altered",
        "empty",
        "absent",
        "unreadable",
    }
    assert frozenset({"timeout", "precondition"}) == LOOT_FAILURE_KINDS


def test_solo_fresh_es_testigo_positivo() -> None:
    positivos = {s for s in LootExecutionWitnessState if LootExecutionWitness(state=s).fresh}
    assert positivos == {LootExecutionWitnessState.FRESH}


def test_diagnostico_no_atribuible_menciona_mutex_como_posibilidad_no_como_hecho() -> None:
    veredicto = classify_loot_sort(
        process_success=True, process_detail="", witness=TESTIGO_INTACTO, before=_ESTADO, after=_ESTADO
    )
    assert veredicto.failure_reason is LootSortFailureReason.EXECUTION_NOT_ATTRIBUTABLE
    assert "LOOT.Shell.Instance" in veredicto.detail
    assert "posible" in veredicto.detail.lower()


# =============================================================================
# Estado semántico del load order (T10/T11/T12 a nivel parser)
# =============================================================================


def test_semantica_ignora_solo_crlf_vacias_y_comentarios() -> None:
    base = parse_load_order_bytes(b"*Skyrim.esm\nMod A.esp\n")
    assert parse_load_order_bytes(b"# cabecera MO2\r\n\r\n*Skyrim.esm\r\n\nMod A.esp") == base
    assert base == ((b"Skyrim.esm", True), (b"Mod A.esp", False))


@pytest.mark.parametrize(
    ("variante", "descripcion"),
    [
        (b"Skyrim.esm\nMod A.esp\n", "activación quitada"),
        (b"*Mod A.esp\n*Skyrim.esm\n", "orden invertido"),
        (b"\xef\xbb\xbf*Skyrim.esm\nMod A.esp\n", "BOM: libloadorder lo lee como parte del nombre"),
        (b"*skyrim.esm\nMod A.esp\n", "mayúsculas: sin case-folding (conservador)"),
        (b"*Skyrim.esm \nMod A.esp\n", "espacio final: libloadorder no hace trim"),
        (b"*Skyrim.esm\nMod A.esp\r", "CR final sin LF: str::lines() lo conserva (LinesMap, Rust 1.82)"),
    ],
)
def test_semantica_preserva_lo_que_libloadorder_preserva(variante: bytes, descripcion: str) -> None:
    assert parse_load_order_bytes(variante) != parse_load_order_bytes(b"*Skyrim.esm\nMod A.esp\n"), descripcion


def test_lectura_semantica_distingue_ausente_de_ilegible(tmp_path: pathlib.Path) -> None:
    ausente = tmp_path / "plugins.txt"
    assert read_load_order_semantics([ausente]) == ((str(ausente), None),)
    directorio = tmp_path / "loadorder.txt"
    directorio.mkdir()
    with pytest.raises(OSError):
        read_load_order_semantics([directorio])


# =============================================================================
# Servicio: T1-T13 (matriz end-to-end bajo el lock real)
# =============================================================================


async def test_t1_changed_con_testigo_fresco_no_revierte(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, loadorder = _load_order(tmp_path)
    nuevo = b"*Skyrim.esm\nMod B.esp\n*Mod A.esp\n"

    def aplicar() -> None:
        plugins.write_bytes(nuevo)

    svc = _servicio(lock_manager, snapshot_manager, _runner(aplicar, resultado=_ok()), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "changed"
    assert res["success"] is True
    assert res["status"] == "success"
    assert res["message"] == ""
    assert "failure_reason" not in res
    assert res["rolled_back"] is False
    assert plugins.read_bytes() == nuevo


async def test_t2_no_change_sin_tocar_archivos_es_exito(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """El camino NO_CHANGE real de 0.29.1: rc 0, log recreado, NINGÚN archivo tocado.

    Ancla central de PR-2: el gate previo ("ningún archivo cambió ⇒ FAIL")
    convertía este caso en fallo + rollback.
    """
    resolver, plugins, loadorder = _load_order(tmp_path)
    mtime_antes = plugins.stat().st_mtime_ns
    svc = _servicio(lock_manager, snapshot_manager, _runner(resultado=_ok()), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "no_change"
    assert res["success"] is True
    assert res["status"] == "success"
    assert res["message"] == ""
    assert "ya estaba ordenado" in res["outcome_detail"]
    assert res["rolled_back"] is False
    assert res["verification"] == {"execution_witness": "fresh", "load_order_rewritten": False}
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL
    assert plugins.stat().st_mtime_ns == mtime_antes


@pytest.mark.parametrize("testigo", [t for t in _TESTIGOS if t is None or not t.fresh], ids=str)
async def test_t3_falso_verde_mutex_rc0_sin_testigo_fresco_falla(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    tmp_path: pathlib.Path,
    testigo: LootExecutionWitness | None,
) -> None:
    resolver, plugins, _ = _load_order(tmp_path)
    svc = _servicio(lock_manager, snapshot_manager, _runner(resultado=_ok(testigo)), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["success"] is False
    assert res["status"] == "error"
    assert res["failure_reason"] == "execution_not_attributable"
    assert res["return_code"] == 0  # la verdad del proceso se conserva
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL


async def test_t4_mutacion_externa_sin_testigo_falla_y_revierte(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """CASE F: rc 0 + load order cambiado + testigo intacto. El cambio NO se
    atribuye a LOOT (un escritor ajeno tocó el archivo en la ventana)."""
    resolver, plugins, _ = _load_order(tmp_path)

    def escritor_ajeno() -> None:
        plugins.write_bytes(b"*Intruso.esp\n")

    svc = _servicio(lock_manager, snapshot_manager, _runner(escritor_ajeno, resultado=_ok(TESTIGO_INTACTO)), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "execution_not_attributable"
    assert "no atribuible" in res["message"]
    assert res["rolled_back"] is True
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL


async def test_t5_rc_no_cero_falla_y_revierte(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, _ = _load_order(tmp_path)

    def corromper() -> None:
        plugins.write_bytes(b"CORRUPTO\n")

    svc = _servicio(lock_manager, snapshot_manager, _runner(corromper, resultado=_ok(return_code=3)), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "process_error"
    assert res["message"] == "LOOT sort failed with exit code 3."
    assert res["rolled_back"] is True
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL


async def test_t6_rc0_con_errores_parseados_falla_y_revierte(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, _ = _load_order(tmp_path)

    def corromper() -> None:
        plugins.write_bytes(b"CORRUPTO\n")

    resultado = _ok(errors=["cyclic interaction detected"])
    svc = _servicio(lock_manager, snapshot_manager, _runner(corromper, resultado=resultado), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "process_error"
    assert res["rolled_back"] is True
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL


async def test_t7_estado_previo_ilegible_no_lanza_loot(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """Un target que existe pero no se puede LEER (acá: es un directorio) deja la
    baseline inverificable: FAIL antes de ejecutar, sin spawn."""
    directorio = tmp_path / "lo"
    directorio.mkdir()
    ilegible = directorio / "loadorder.txt"
    ilegible.mkdir()
    resolver = MagicMock()
    resolver.resolve.return_value = LoadOrderPaths(files=(ilegible,), sources=("override",))
    runner = _runner(resultado=_ok())
    svc = _servicio(lock_manager, snapshot_manager, runner, resolver)

    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "state_unobservable"
    assert res["return_code"] == -1
    runner.sort.assert_not_awaited()


async def test_t8_estado_final_ilegible_falla_y_revierte(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, _ = _load_order(tmp_path)

    def corromper() -> None:
        plugins.write_bytes(b"*Otro.esp\n")

    real = loot_service_module.read_load_order_semantics
    llamadas = {"n": 0}

    def lectura(paths: Any) -> Any:
        llamadas["n"] += 1
        if llamadas["n"] >= 2:  # la lectura POST
            raise loot_service_module.LoadOrderUnobservableError(plugins, PermissionError("denied"))
        return real(paths)

    svc = _servicio(lock_manager, snapshot_manager, _runner(corromper, resultado=_ok()), resolver)
    with patch.object(loot_service_module, "read_load_order_semantics", lectura):
        res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "state_unobservable"
    assert res["rolled_back"] is True
    assert res["verification"]["load_order_rewritten"] is None
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL


async def test_t9_sin_targets_no_lanza_loot(lock_manager, snapshot_manager) -> None:  # noqa: ANN001
    resolver = MagicMock()
    resolver.resolve.return_value = LoadOrderPaths(files=(), sources=())
    runner = _runner(resultado=_ok())
    svc = _servicio(lock_manager, snapshot_manager, runner, resolver)

    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "precondition_failed"
    assert res["rolled_back"] is False
    runner.sort.assert_not_awaited()


async def test_t10_reescritura_solo_metadata_es_no_change(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """Bytes y mtime cambian (CRLF, comentario y líneas vacías nuevas) pero el
    load order semántico es idéntico → NO_CHANGE, y la telemetría física lo ve."""
    resolver, plugins, loadorder = _load_order(tmp_path)

    def reescribir() -> None:
        plugins.write_bytes(b"# regenerado\r\n*Skyrim.esm\r\n\r\n*Mod A.esp\r\nMod B.esp\r\n")
        loadorder.write_bytes(b"# regenerado\nSkyrim.esm\n\nMod A.esp\nMod B.esp")
        os.utime(plugins, ns=(2_000_000_000, 2_000_000_000))

    svc = _servicio(lock_manager, snapshot_manager, _runner(reescribir, resultado=_ok()), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "no_change"
    assert res["success"] is True
    assert res["verification"]["load_order_rewritten"] is True
    assert res["rolled_back"] is False


async def test_t11_cambio_de_activacion_es_changed(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, plugins, _ = _load_order(tmp_path)

    def desactivar() -> None:
        plugins.write_bytes(_PLUGINS_ORIGINAL.replace(b"*Mod A.esp", b"Mod A.esp"))

    svc = _servicio(lock_manager, snapshot_manager, _runner(desactivar, resultado=_ok()), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "changed"


async def test_t12_cambio_de_orden_en_loadorder_es_changed(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, _, loadorder = _load_order(tmp_path)

    def reordenar() -> None:
        loadorder.write_bytes(b"Skyrim.esm\nMod B.esp\nMod A.esp\n")

    svc = _servicio(lock_manager, snapshot_manager, _runner(reordenar, resultado=_ok()), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "changed"


async def test_t13_stdout_vacio_y_sorted_plugins_vacio_es_no_change(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """LOOT GUI real completa ``--auto-sort`` sin imprimir nada: vacío ≠ error."""
    resolver, _, _ = _load_order(tmp_path)
    resultado = _ok(sorted_plugins=[], raw_stdout="", raw_stderr="")
    svc = _servicio(lock_manager, snapshot_manager, _runner(resultado=resultado), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "no_change"
    assert res["success"] is True
    assert res["sorted_plugins"] == []


# =============================================================================
# T14 — "Sorting operation complete." NO es oráculo de éxito
# =============================================================================


def test_t14_marcador_anexado_al_sentinel_no_es_testigo(tmp_path: pathlib.Path) -> None:
    """Un escritor que ANEXA líneas (incluido el marcador de finalización) al
    sentinel no recreó el log: el ciclo de vida upstream siempre borra y recrea
    (loot_state.cpp:105-106). SENTINEL_ALTERED, nunca FRESH."""
    armado = arm_execution_witness(tmp_path)
    with armado.log_path.open("ab") as handle:
        handle.write(b"[12:00:00.000000] [info]: Sorting operation complete.\n")
    assert armado.observe().state is LootExecutionWitnessState.SENTINEL_ALTERED


async def test_t14_marcador_en_log_fresco_con_rc_no_cero_es_fail(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """Upstream loguea el marcador INCONDICIONALMENTE (sort_plugins_query.h),
    incluso tras 'Failed to sort plugins' (game.cpp:885-890): con fallo de
    proceso el veredicto es FAIL aunque el log recreado lo contenga."""
    resolver, _, _ = _load_order(tmp_path)
    log = (
        "[12:00:00.1] [error]: Failed to sort plugins. Details: boom\n"
        "[12:00:00.2] [info]: Sorting operation complete.\n"
    )
    resultado = _ok(return_code=1, raw_stdout=log)
    svc = _servicio(lock_manager, snapshot_manager, _runner(resultado=resultado), resolver)
    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert res["failure_reason"] == "process_error"


def _constantes_de_codigo(arbol: ast.AST) -> list[str]:
    """Strings literales del módulo EXCEPTO docstrings (documentar ≠ usar)."""
    docstrings: set[int] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            cuerpo = nodo.body
            if cuerpo and isinstance(cuerpo[0], ast.Expr) and isinstance(cuerpo[0].value, ast.Constant):
                docstrings.add(id(cuerpo[0].value))
    return [
        nodo.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in docstrings
    ]


def test_t14_ningun_codigo_de_produccion_usa_el_marcador_como_oraculo() -> None:
    """Ancla estructural (enumera TODO ``sky_claw/``): el marcador de finalización
    sólo puede aparecer en documentación, nunca como literal de código."""
    culpables = [
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*.py")
        if any(
            "Sorting operation complete" in valor
            for valor in _constantes_de_codigo(ast.parse(archivo.read_text(encoding="utf-8")))
        )
    ]
    assert culpables == []


# =============================================================================
# T15-T17 — semántica transaccional (journal)
# =============================================================================


async def _estado_ultima_tx(journal: Any) -> Any:
    (ultima,) = await journal.list_recent_transactions(limit=1)
    return ultima


async def _informe(journal: Any) -> Any:
    from sky_claw.app.orchestrator.preview.manifest import FlightReport

    ultima = await _estado_ultima_tx(journal)
    ops = await journal.get_operations_by_transaction(ultima.transaction_id)
    informes = [
        FlightReport.model_validate(e.metadata) for e in ops if e.metadata and e.metadata.get("kind") == "flight_report"
    ]
    assert len(informes) == 1
    return informes[0]


async def test_t15_no_change_commitea_no_revierte(lock_manager, snapshot_manager, journal, tmp_path) -> None:  # noqa: ANN001
    from sky_claw.app.db.journal import TransactionStatus

    resolver, _, _ = _load_order(tmp_path)
    svc = _servicio(lock_manager, snapshot_manager, _runner(resultado=_ok()), resolver, journal)
    res = await svc.sort_load_order()

    assert res["outcome"] == "no_change"
    assert res["rolled_back"] is False
    assert (await _estado_ultima_tx(journal)).status == TransactionStatus.COMMITTED
    # NO_CHANGE sin diff inventado.
    assert (await _informe(journal)).load_order_diff is None


async def test_t16_changed_commitea_con_diff_real(lock_manager, snapshot_manager, journal, tmp_path) -> None:  # noqa: ANN001
    from sky_claw.app.db.journal import TransactionStatus

    resolver, _, loadorder = _load_order(tmp_path)

    def reordenar() -> None:
        loadorder.write_bytes(b"Skyrim.esm\nMod B.esp\nMod A.esp\n")

    svc = _servicio(lock_manager, snapshot_manager, _runner(reordenar, resultado=_ok()), resolver, journal)
    res = await svc.sort_load_order()

    assert res["outcome"] == "changed"
    assert (await _estado_ultima_tx(journal)).status == TransactionStatus.COMMITTED
    diff = (await _informe(journal)).load_order_diff
    assert diff is not None
    assert diff.changed


@pytest.mark.parametrize(
    "resultado",
    [_ok(TESTIGO_INTACTO), _ok(return_code=1), _ok(errors=["boom"])],
    ids=["no_atribuible", "rc1", "errores"],
)
async def test_t17_fail_revierte_la_transaccion(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    journal,  # noqa: ANN001
    tmp_path: pathlib.Path,
    resultado: LOOTResult,
) -> None:
    from sky_claw.app.db.journal import TransactionStatus

    resolver, _, _ = _load_order(tmp_path)
    svc = _servicio(lock_manager, snapshot_manager, _runner(resultado=resultado), resolver, journal)
    res = await svc.sort_load_order()

    assert res["outcome"] == "fail"
    assert (await _estado_ultima_tx(journal)).status == TransactionStatus.ROLLED_BACK


# =============================================================================
# LOOTRunner: testigo capturado alrededor del proceso (proceso fake)
# =============================================================================


def _loot_exe(tmp_path: pathlib.Path, version: tuple[int, int, int] = (0, 29, 1)) -> pathlib.Path:
    """PE con ``VERSIONINFO`` real: sin versión atestiguable el runner no lanza LOOT."""
    return escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe", version)


def _proceso_fake(comportamiento: Any, *, returncode: int = 0, stdout: bytes = b"") -> Any:
    """``create_subprocess_exec`` fake: ``comportamiento(argv)`` corre dentro de
    ``communicate()``, es decir MIENTRAS 'LOOT' vive — como el binario real."""
    capturado: dict[str, Any] = {"eventos": []}

    async def fake_exec(*args: object, **_kwargs: object) -> MagicMock:
        argv = [str(a) for a in args]
        capturado["argv"] = argv
        capturado["eventos"].append("spawn")
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = None

        async def communicate() -> tuple[bytes, bytes]:
            comportamiento(argv)
            capturado["eventos"].append("exit")
            proc.returncode = returncode
            return stdout, b""

        proc.communicate = communicate
        return proc

    return capturado, fake_exec


def _data_root(argv: list[str]) -> pathlib.Path:
    return pathlib.Path(argv[argv.index("--loot-data-path") + 1])


def _loot_que_supera_el_mutex(argv: list[str]) -> None:
    """Fiel a LootState::LootState (loot_state.cpp:105-106) + logRuntimeEnvironment."""
    log = _data_root(argv) / LOOT_DEBUG_LOG_FILENAME
    log.unlink(missing_ok=True)
    log.write_bytes(b"[12:00:00.000001] [info]: Running 64-bit LOOT on Windows 11\n")


def _segunda_instancia(_argv: list[str]) -> None:
    """Fiel a main.cpp:90-95: sale antes de inicializar nada."""


async def _correr_runner(tmp_path: pathlib.Path, comportamiento: Any, **kwargs: Any) -> tuple[LOOTResult, Any]:
    root = tmp_path / "loot_data"
    root.mkdir(exist_ok=True)
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path, loot_data_path=root))
    capturado, fake_exec = _proceso_fake(comportamiento, **kwargs)
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        resultado = await runner.sort()
    return resultado, capturado


async def test_runner_loot_que_supera_el_mutex_deja_testigo_fresco(tmp_path: pathlib.Path) -> None:
    resultado, _ = await _correr_runner(tmp_path, _loot_que_supera_el_mutex)
    assert resultado.success is True
    assert resultado.execution_witness == TESTIGO_FRESCO


async def test_runner_crea_el_data_root_si_falta_antes_de_armar(tmp_path: pathlib.Path) -> None:
    """Camino directo: el root puede no existir todavía (LOOT lo crearía, pero el
    settings y el sentinel se escriben ANTES del spawn)."""
    root = tmp_path / "nuevo" / "loot_data"
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path, loot_data_path=root))
    _, fake_exec = _proceso_fake(_loot_que_supera_el_mutex)
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        resultado = await runner.sort()
    assert resultado.execution_witness == TESTIGO_FRESCO
    assert (root / "settings.toml").read_text(encoding="utf-8") == _GESTIONADO


async def test_runner_segunda_instancia_deja_sentinel_intacto(tmp_path: pathlib.Path) -> None:
    """R3 simulado: rc 0 sin runtime. El éxito de PROCESO no cambia (el parser no
    puede distinguirlo) pero el testigo sí."""
    resultado, _ = await _correr_runner(tmp_path, _segunda_instancia)
    assert resultado.success is True  # nivel proceso/parser: rc 0, sin errores
    assert resultado.execution_witness is not None
    assert resultado.execution_witness.state is LootExecutionWitnessState.SENTINEL_INTACT


async def test_runner_log_previo_de_otra_corrida_no_cuenta_como_fresco(tmp_path: pathlib.Path) -> None:
    """``log.exists()`` NO es evidencia: un log viejo (incluso con el marcador de
    finalización) se reemplaza por el sentinel antes del spawn."""
    root = tmp_path / "loot_data"
    root.mkdir()
    (root / LOOT_DEBUG_LOG_FILENAME).write_bytes(b"[09:00:00.0] [info]: Sorting operation complete.\n")
    resultado, _ = await _correr_runner(tmp_path, _segunda_instancia)
    assert resultado.execution_witness is not None
    assert resultado.execution_witness.state is LootExecutionWitnessState.SENTINEL_INTACT


@pytest.mark.parametrize(
    ("comportamiento", "estado"),
    [
        (lambda argv: (_data_root(argv) / LOOT_DEBUG_LOG_FILENAME).unlink(), LootExecutionWitnessState.ABSENT),
        (lambda argv: (_data_root(argv) / LOOT_DEBUG_LOG_FILENAME).write_bytes(b""), LootExecutionWitnessState.EMPTY),
    ],
    ids=["borrado_sin_recrear", "recreado_vacio"],
)
async def test_runner_ciclos_de_vida_anomalos_no_son_frescos(
    tmp_path: pathlib.Path, comportamiento: Any, estado: LootExecutionWitnessState
) -> None:
    resultado, _ = await _correr_runner(tmp_path, comportamiento)
    assert resultado.execution_witness is not None
    assert resultado.execution_witness.state is estado
    assert not resultado.execution_witness.fresh


async def test_runner_observa_el_testigo_despues_de_que_el_proceso_termina(tmp_path: pathlib.Path) -> None:
    """Orden de captura: arm → spawn → communicate/exit → observe."""
    eventos: list[str] = []
    real_arm = witness_module.arm_execution_witness

    def arm_espia(path: pathlib.Path) -> Any:
        eventos.append("arm")
        armado = real_arm(path)

        class _Espia:
            def observe(self) -> LootExecutionWitness:
                eventos.append("observe")
                return armado.observe()

        return _Espia()

    with patch("sky_claw.local.loot.cli.arm_execution_witness", arm_espia):
        _, capturado = await _correr_runner(tmp_path, _loot_que_supera_el_mutex)
    orden = [eventos[0], *capturado["eventos"], eventos[1]]
    assert orden == ["arm", "spawn", "exit", "observe"]


async def test_runner_sin_poder_armar_el_testigo_no_lanza_loot(tmp_path: pathlib.Path) -> None:
    """Windows: un LOOT huérfano con el log abierto sin FILE_SHARE_DELETE hace
    fallar el ``os.replace`` del sentinel → precondición, sin spawn."""
    root = tmp_path / "loot_data"
    root.mkdir()
    # Settings ya gestionado (UNCHANGED, sin escritura): el único os.replace de la
    # preparación es el del sentinel.
    ensure_loot_headless_settings(root, loot_version=_VERSION)
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path, loot_data_path=root))
    fake_exec = AsyncMock()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
        patch.object(witness_module.os, "replace", side_effect=PermissionError("sharing violation")),
        pytest.raises(LOOTPreconditionError, match="testigo"),
    ):
        await runner.sort()
    fake_exec.assert_not_awaited()
    assert list(root.glob("*.tmp")) == []  # el temporal no queda huérfano


async def test_runner_settings_invalido_no_lanza_loot_ni_lo_pisa(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "loot_data"
    root.mkdir()
    settings = root / "settings.toml"
    settings.write_bytes(b"this is = = not toml\n")
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path, loot_data_path=root))
    fake_exec = AsyncMock()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
        pytest.raises(LOOTPreconditionError, match="TOML"),
    ):
        await runner.sort()
    fake_exec.assert_not_awaited()
    assert settings.read_bytes() == b"this is = = not toml\n"


async def test_runner_timeout_mata_reapea_y_no_observa(tmp_path: pathlib.Path) -> None:
    """El hardening de timeout no cambió: kill + wait, LOOTTimeoutError y SIN
    observación del testigo (no hay proceso terminado que atribuir)."""
    root = tmp_path / "loot_data"
    root.mkdir()
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path, loot_data_path=root, timeout=0))

    async def communicate_bloqueado() -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""

    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=communicate_bloqueado)
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=-9)
    observado = MagicMock()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=proc),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
        patch.object(witness_module.ArmedExecutionWitness, "observe", observado),
        pytest.raises(LOOTTimeoutError, match="timed out after 0s"),
    ):
        await runner.sort()
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited_once_with()
    observado.assert_not_called()


async def test_runner_cancelado_mata_reapea_y_repropaga(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "loot_data"
    root.mkdir()
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path, loot_data_path=root))
    iniciado = asyncio.Event()

    async def communicate_bloqueado() -> tuple[bytes, bytes]:
        iniciado.set()
        await asyncio.Event().wait()
        return b"", b""

    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=communicate_bloqueado)
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=-9)
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=proc),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        tarea = asyncio.create_task(runner.sort())
        await asyncio.wait_for(iniciado.wait(), timeout=1.0)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited_once_with()


# =============================================================================
# T18-T20 — invariantes de PR-0 / PR-1 / F8 intactos
# =============================================================================


async def test_t18_pr0_identificador_de_juego_exacto(tmp_path: pathlib.Path) -> None:
    _, capturado = await _correr_runner(tmp_path, _loot_que_supera_el_mutex)
    argv = capturado["argv"]
    assert argv[argv.index("--game") + 1] == "Skyrim Special Edition"
    assert argv[-1] == "--auto-sort"
    assert "--update-masterlist" not in argv


async def test_t19_pr1_data_root_explicito_y_unico_lugar_escrito(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "loot_data"
    _, capturado = await _correr_runner(tmp_path, _loot_que_supera_el_mutex)
    argv = capturado["argv"]
    assert argv[argv.index("--loot-data-path") + 1] == str(root)
    # Lo único que Sky-Claw prepara, y SOLO dentro del data root aislado.
    assert sorted(p.name for p in root.iterdir()) == [LOOT_DEBUG_LOG_FILENAME, "settings.toml"]


async def test_t19_runner_sin_data_root_no_escribe_nada_ni_produce_testigo(tmp_path: pathlib.Path) -> None:
    """Runner legacy (sin --loot-data-path): jamás se toca el root del GUI del
    operador; el resultado sale sin testigo y el servicio nunca lo atribuye."""
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path), game_path=tmp_path))
    antes = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    capturado, fake_exec = _proceso_fake(_segunda_instancia)
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        resultado = await runner.sort()
    assert "--loot-data-path" not in capturado["argv"]
    assert resultado.execution_witness is None
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == antes


def test_t20_f8_loot_interno_de_mo2_sigue_rechazado(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner, VfsRequiredLootRunner, build_vfs_loot_runner

    mo2 = tmp_path / "MO2"
    interno = mo2 / "loot" / "lootcli.exe"
    interno.parent.mkdir(parents=True)
    interno.write_bytes(b"x")
    data = tmp_path / "Skyrim" / "Data"
    data.mkdir(parents=True)
    with pytest.raises(ValueError, match="F8 guard"):
        BrokeredLootRunner(
            broker=MagicMock(),
            instance_id="portable-main",
            mo2_root=mo2,
            profile="Default",
            game_data_dir=data,
            loot_exe=interno,
            timeout=120,
            mutation_targets=tuple,
            loot_data_path=tmp_path / "loot_data",
        )
    guard = build_vfs_loot_runner(
        broker=MagicMock(),
        instance_id="portable-main",
        mo2_root=mo2,
        game_path=data.parent,
        loot_exe=interno,
        profile="Default",
        loot_data_path=tmp_path / "loot_data",
    )
    assert isinstance(guard, VfsRequiredLootRunner)


# =============================================================================
# Frontera worker → broker: el testigo viaja como DATO con schema cerrado
# =============================================================================


def test_payload_del_testigo_es_json_y_roundtrip_exacto() -> None:
    for estado in LootExecutionWitnessState:
        testigo = LootExecutionWitness(state=estado)
        payload = json.loads(json.dumps(testigo.to_payload()))
        assert payload == {"schema": 1, "state": estado.value}
        assert LootExecutionWitness.from_payload(payload) == testigo


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "fresh",
        ["fresh"],
        {},
        {"state": "fresh"},
        {"schema": 1},
        {"schema": 1, "state": "fresh", "extra": True},
        {"schema": 2, "state": "fresh"},
        {"schema": True, "state": "fresh"},
        {"schema": "1", "state": "fresh"},
        {"schema": 1, "state": "FRESH"},
        {"schema": 1, "state": 1},
    ],
    ids=repr,
)
def test_payload_del_testigo_fuera_de_schema_se_rechaza(payload: object) -> None:
    with pytest.raises(LootExecutionWitnessPayloadError):
        LootExecutionWitness.from_payload(payload)


def _manifest_worker(tmp_path: pathlib.Path) -> Any:
    from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
    from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob
    from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest

    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    game_data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    game_data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    target = profile / "plugins.txt"
    target.write_bytes(b"*Skyrim.esm\n")
    loot_data = tmp_path / "loot_data" / "portable-main" / "Default"
    loot_data.mkdir(parents=True)
    challenge = build_attestation_challenge(data_root=mo2, profile="Default", physical_data_dir=game_data)
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="loot_sort",
        payload={
            "loot_exe": str(_loot_exe(tmp_path)),
            "game": "SkyrimSE",
            "update_masterlist": False,
            "loot_data_path": str(loot_data),
        },
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(target.resolve(),),
    )
    return VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        mo2_root=mo2,
        virtual_data_dir=game_data,
        descriptor_path=tmp_path / "descriptor.json",
    )


async def test_worker_serializa_el_testigo_capturado_por_el_runner(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.mo2.vfs_worker import _loot_handler

    manifest = _manifest_worker(tmp_path)
    _, fake_exec = _proceso_fake(_loot_que_supera_el_mutex)
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        execution = await _loot_handler(manifest)
    assert execution.success is True
    assert execution.tool_result["execution_witness"] == {"schema": 1, "state": "fresh"}
    json.dumps(execution.tool_result)  # serializable por IPC


@pytest.mark.parametrize(
    ("excepcion", "kind", "outputs_declarados"),
    [(LOOTTimeoutError(10), "timeout", True), (LOOTPreconditionError("settings"), "precondition", False)],
    ids=["timeout", "precondition"],
)
async def test_worker_tipa_timeout_y_precondicion(
    tmp_path: pathlib.Path, excepcion: Exception, kind: str, outputs_declarados: bool
) -> None:
    from sky_claw.local.mo2.vfs_worker import _loot_handler

    manifest = _manifest_worker(tmp_path)
    with patch("sky_claw.local.mo2.vfs_worker.LOOTRunner.sort", AsyncMock(side_effect=excepcion)):
        execution = await _loot_handler(manifest)
    assert execution.success is False
    assert execution.exit_code is None
    assert execution.tool_result == {"loot_failure_kind": kind}
    assert bool(execution.outputs) is outputs_declarados


def _runner_brokered(tmp_path: pathlib.Path, broker: Any) -> Any:
    from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner

    mo2 = tmp_path / "MO2b"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    data = tmp_path / "SkyrimB" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    target = profile / "plugins.txt"
    target.write_bytes(b"*Skyrim.esm\n")
    loot_data = tmp_path / "loot_data_b" / "portable-main" / "Default"
    loot_data.mkdir(parents=True)
    return BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=_loot_exe(tmp_path),
        timeout=120,
        mutation_targets=lambda: (target,),
        loot_data_path=loot_data,
    )


class _BrokerConResultado:
    def __init__(self, *, success: bool = True, tool_result: dict[str, Any] | None = None) -> None:
        self._success = success
        self._tool_result = tool_result or {}

    async def submit(self, job: Any, **_kwargs: object) -> Any:
        from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJobResult

        return VfsJobResult.from_dict(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "job_id": job.job_id,
                "success": self._success,
                "message": "" if self._success else "detalle del worker",
                "exit_code": 0 if self._success else None,
                "stdout": "",
                "stderr": "",
                "outputs": [],
                "rollback_state": "not_required" if self._success else "pending",
                "attestation": None,
                "tool_result": self._tool_result,
            }
        )


@pytest.mark.parametrize(
    ("tool_result", "esperado"),
    [
        ({"execution_witness": {"schema": 1, "state": "fresh"}}, TESTIGO_FRESCO),
        ({"execution_witness": {"schema": 1, "state": "sentinel_intact"}}, TESTIGO_INTACTO),
        ({}, None),
    ],
    ids=["fresco", "intacto", "ausente"],
)
async def test_broker_valida_el_testigo_con_schema_cerrado(
    tmp_path: pathlib.Path, tool_result: dict[str, Any], esperado: LootExecutionWitness | None
) -> None:
    runner = _runner_brokered(tmp_path, _BrokerConResultado(tool_result=tool_result))
    resultado = await runner.sort()
    assert resultado.execution_witness == esperado


@pytest.mark.parametrize(
    "testigo",
    [{"schema": 1, "state": "fresh", "forged": True}, "fresh", {"schema": 2, "state": "fresh"}],
    ids=["campo_extra", "no_objeto", "schema_futuro"],
)
async def test_broker_testigo_presente_e_invalido_es_error_de_protocolo(tmp_path: pathlib.Path, testigo: Any) -> None:
    """Un testigo PRESENTE fuera del schema v1 no es "falta de evidencia" (cuyo
    diagnóstico apuntaría al mutex): es un mensaje fuera de contrato."""
    runner = _runner_brokered(tmp_path, _BrokerConResultado(tool_result={"execution_witness": testigo}))
    with pytest.raises(LOOTWorkerProtocolError, match="schema cerrado v1"):
        await runner.sort()


async def test_broker_relanza_timeout_y_precondicion_tipados(tmp_path: pathlib.Path) -> None:
    runner = _runner_brokered(
        tmp_path, _BrokerConResultado(success=False, tool_result={"loot_failure_kind": "timeout"})
    )
    with pytest.raises(LOOTTimeoutError):
        await runner.sort()
    runner = _runner_brokered(
        tmp_path / "b", _BrokerConResultado(success=False, tool_result={"loot_failure_kind": "precondition"})
    )
    with pytest.raises(LOOTPreconditionError, match="detalle del worker"):
        await runner.sort()


@pytest.mark.parametrize(
    ("success", "kind"),
    [(False, "desconocido"), (False, 7), (True, "timeout"), (True, "precondition")],
    ids=["kind_desconocido", "kind_no_string", "timeout_con_success_true", "precondition_con_success_true"],
)
async def test_broker_kind_inconsistente_es_error_de_protocolo(
    tmp_path: pathlib.Path, success: bool, kind: Any
) -> None:
    """Finding de review (#634): ``success=True`` + ``loot_failure_kind`` NO se
    re-tipa a timeout/precondición (sería creerle al kind de un mensaje
    contradictorio) ni se degrada a PROCESS_ERROR genérico: error de protocolo."""
    runner = _runner_brokered(tmp_path, _BrokerConResultado(success=success, tool_result={"loot_failure_kind": kind}))
    with pytest.raises(LOOTWorkerProtocolError, match="contradictorio"):
        await runner.sort()


async def test_broker_traduce_su_timeout_a_loot_timeout(tmp_path: pathlib.Path) -> None:
    """En un cuelgue real el timeout del broker dispara antes que el del worker:
    sin la traducción, el camino productivo reportaría un error genérico."""
    from sky_claw.local.mo2.vfs_broker import VfsJobTimeoutError

    broker = MagicMock()
    broker.submit = AsyncMock(side_effect=VfsJobTimeoutError("job excedió 120s"))
    runner = _runner_brokered(tmp_path, broker)
    with pytest.raises(LOOTTimeoutError):
        await runner.sort()


# =============================================================================
# Contrato de respuesta: TODA salida lleva outcome; hermanos GUI/agente
# =============================================================================


def test_toda_salida_de_sort_load_order_lleva_outcome() -> None:
    """Ancla por AST: cada ``return`` de ``sort_load_order`` devuelve la respuesta
    del veredicto o la fábrica ``_respuesta_de_fallo`` — ningún dict a mano que
    pueda olvidar ``outcome``/``failure_reason``."""
    fuente = (PAQUETE / "local" / "tools" / "loot_service.py").read_text(encoding="utf-8")
    funcion = next(
        nodo
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "sort_load_order"
    )
    retornos = [nodo for nodo in ast.walk(funcion) if isinstance(nodo, ast.Return)]
    assert retornos, "sort_load_order debe tener returns"
    for retorno in retornos:
        valor = retorno.value
        es_respuesta = isinstance(valor, ast.Name) and valor.id == "response"
        es_fabrica = (
            isinstance(valor, ast.Call) and isinstance(valor.func, ast.Name) and valor.func.id == "_respuesta_de_fallo"
        )
        assert es_respuesta or es_fabrica, ast.unparse(retorno)


@pytest.mark.parametrize(
    ("excepcion", "razon"),
    [
        (LOOTTimeoutError(120), "timeout"),
        (LOOTNotFoundError("sin LOOT"), "precondition_failed"),
        (LOOTPreconditionError("testigo"), "precondition_failed"),
        (LOOTWorkerProtocolError("kind con success=True"), "protocol_error"),
        (RuntimeError("kaput"), "process_error"),
    ],
    ids=["timeout", "not_found", "precondition", "protocolo", "inesperado"],
)
async def test_salidas_por_excepcion_llevan_outcome_y_razon(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    tmp_path: pathlib.Path,
    excepcion: Exception,
    razon: str,
) -> None:
    resolver, plugins, _ = _load_order(tmp_path)
    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=excepcion)
    svc = _servicio(lock_manager, snapshot_manager, runner, resolver)
    res = await svc.sort_load_order()
    assert res["outcome"] == "fail"
    assert res["success"] is False
    assert res["failure_reason"] == razon
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL


async def test_timeout_diagnostica_causas_sin_afirmar_una(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, _, _ = _load_order(tmp_path)
    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=LOOTTimeoutError(120))
    res = await _servicio(lock_manager, snapshot_manager, runner, resolver).sort_load_order()
    assert res["message"] == "LOOT timed out after 120s"
    assert "no distingue la causa" in res["outcome_detail"]


async def test_preflight_rojo_y_lock_ocupado_llevan_outcome(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID

    resolver, _, _ = _load_order(tmp_path)
    reporte = MagicMock()
    reporte.blocks_mutations = True
    reporte.checks = []
    reporte.to_dict.return_value = {"status": "red"}
    preflight = MagicMock()
    preflight.run = AsyncMock(return_value=reporte)
    preflight.loot_version = None
    svc = LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=_runner(resultado=_ok()),
        load_order_resolver=resolver,
        preflight=preflight,
    )
    rojo = await svc.sort_load_order()
    assert (rojo["outcome"], rojo["failure_reason"]) == ("fail", "precondition_failed")

    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "otro", ttl=30.0)
    ocupado = await _servicio(lock_manager, snapshot_manager, _runner(resultado=_ok()), resolver).sort_load_order()
    assert (ocupado["outcome"], ocupado["failure_reason"]) == ("fail", "precondition_failed")


async def test_no_change_no_se_convierte_en_error_en_ninguna_superficie(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO_CHANGE es éxito para el normalizador canónico, el toast de la GUI y el
    JSON del agente LLM (que reconstruye el dict y podría perder ``outcome``)."""
    from sky_claw.app.agent.tools.system_tools import run_loot_sort
    from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result
    from sky_claw.local.tools.tool_result import normalize_tool_result

    resolver, _, _ = _load_order(tmp_path)
    res = await _servicio(lock_manager, snapshot_manager, _runner(resultado=_ok()), resolver).sort_load_order()
    assert normalize_tool_result(res)["success"] is True
    assert normalize_tool_result(res)["message"] == ""
    assert summarize_ritual_result("execute_loot_sorting", res)[1] == "positive"

    # Agente LLM: mismo servicio vía run_loot_sort con el load order en LOCALAPPDATA.
    la = tmp_path / "localappdata"
    game_dir = la / "Skyrim Special Edition"
    game_dir.mkdir(parents=True)
    (game_dir / "plugins.txt").write_bytes(_PLUGINS_ORIGINAL)
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    agente = json.loads(
        await run_loot_sort(
            MagicMock(),
            _runner(resultado=_ok()),
            None,
            profile="Default",
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )
    assert agente["success"] is True
    assert agente["outcome"] == "no_change"
    assert "failure_reason" not in agente


async def test_agente_recibe_la_razon_tipada_del_fail(lock_manager, snapshot_manager, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from sky_claw.app.agent.tools.system_tools import run_loot_sort

    la = tmp_path / "localappdata"
    game_dir = la / "Skyrim Special Edition"
    game_dir.mkdir(parents=True)
    (game_dir / "plugins.txt").write_bytes(_PLUGINS_ORIGINAL)
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    agente = json.loads(
        await run_loot_sort(
            MagicMock(),
            _runner(resultado=_ok(TESTIGO_INTACTO)),
            None,
            profile="Default",
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )
    assert agente["success"] is False
    assert agente["outcome"] == "fail"
    assert agente["failure_reason"] == "execution_not_attributable"


# =============================================================================
# S1-S6 — política de settings headless (evidencia: loot_settings.h:126-131,
# loot_settings.cpp:843-854, main_window.cpp:366-368/405-416/1600-1613)
# =============================================================================

#: Adversarial a propósito: LOOT NO escribe ``useNoSortingChangesDialog`` dentro
#: de ``[[games]]`` (``LootSettings::save``, loot_settings.cpp:1010-1044, sólo
#: gameId/name/folder/master/minimumHeaderVersion/masterlistSource/path/
#: local_path/hiddenMessages). La clave homónima está para probar que la
#: gestionada es SÓLO la top-level, nunca para afirmar que LOOT la genera.
_SETTINGS_LOOT = (
    "enableDebugLogging = false\n"
    "updateMasterlist = true\n"
    "enableLootUpdateCheck = true\n"
    "useNoSortingChangesDialog = true\n"
    'game = "Skyrim Special Edition"\n'
    'language = "en"\n'
    'lastVersion = "0.29.1"\n'
    'preludeSource = "https://raw.githubusercontent.com/loot/prelude/v0.26/prelude.yaml"\n'
    "\n"
    "[filters]\n"
    "hideBashTags = true\n"
    "\n"
    "[[games]]\n"
    'name = "Skyrim Special Edition"\n'
    'folder = "Skyrim Special Edition"\n'
    'masterlistSource = "https://raw.githubusercontent.com/loot/skyrimse/v0.26/masterlist.yaml"\n'
    "useNoSortingChangesDialog = true\n"
)


def test_s1_root_nuevo_recibe_los_settings_exactos(tmp_path: pathlib.Path) -> None:
    assert ensure_loot_headless_settings(tmp_path, loot_version=_VERSION) is LootHeadlessSettingsAction.CREATED
    assert (tmp_path / "settings.toml").read_text(encoding="utf-8") == _GESTIONADO


def test_s2_idempotente_no_reescribe(tmp_path: pathlib.Path) -> None:
    ensure_loot_headless_settings(tmp_path, loot_version=_VERSION)
    settings = tmp_path / "settings.toml"
    # 2 s exactos: representable sin redondeo en NTFS (100 ns) y FAT (2 s).
    marca = 2_000_000_000
    os.utime(settings, ns=(marca, marca))
    assert ensure_loot_headless_settings(tmp_path, loot_version=_VERSION) is LootHeadlessSettingsAction.UNCHANGED
    assert settings.stat().st_mtime_ns == marca
    assert settings.read_text(encoding="utf-8") == _GESTIONADO


def test_s3_s6_claves_ajenas_y_masterlist_sobreviven_byte_a_byte(tmp_path: pathlib.Path) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_text(_SETTINGS_LOOT, encoding="utf-8")
    antes = tomllib.loads(_SETTINGS_LOOT)

    assert ensure_loot_headless_settings(tmp_path, loot_version=_VERSION) is LootHeadlessSettingsAction.UPDATED

    texto = settings.read_text(encoding="utf-8")
    esperado = _SETTINGS_LOOT.replace("enableLootUpdateCheck = true\n", "enableLootUpdateCheck = false\n", 1).replace(
        "useNoSortingChangesDialog = true\ngame", "useNoSortingChangesDialog = false\ngame", 1
    )
    assert texto == esperado  # sólo cambiaron los valores de las claves top-level gestionadas
    despues = tomllib.loads(texto)
    assert despues["useNoSortingChangesDialog"] is False
    assert despues["enableLootUpdateCheck"] is False
    assert despues["lastVersion"] == _VERSION  # ya coincidía: su línea no se tocó
    # S6: masterlist/prelude/updateMasterlist intactos (incluida la clave homónima
    # DENTRO de [[games]], que no es la gestionada).
    for clave in ("updateMasterlist", "preludeSource", "filters", "games"):
        assert despues[clave] == antes[clave], clave


def test_s3_clave_ausente_se_antepone_sin_tocar_el_resto(tmp_path: pathlib.Path) -> None:
    settings = tmp_path / "settings.toml"
    original = _SETTINGS_LOOT.replace("useNoSortingChangesDialog = true\ngame", "game", 1).replace(
        "enableLootUpdateCheck = true", "enableLootUpdateCheck = false", 1
    )
    settings.write_text(original, encoding="utf-8")
    assert ensure_loot_headless_settings(tmp_path, loot_version=_VERSION) is LootHeadlessSettingsAction.UPDATED
    assert settings.read_text(encoding="utf-8") == "useNoSortingChangesDialog = false\n" + original


def test_s3_clave_ausente_en_archivo_crlf_respeta_el_salto(tmp_path: pathlib.Path) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_bytes(b'game = "x"\r\n[filters]\r\nhideCRCs = true\r\n')
    ensure_loot_headless_settings(tmp_path, loot_version=_VERSION)
    assert settings.read_bytes() == (
        b"useNoSortingChangesDialog = false\r\nenableLootUpdateCheck = false\r\n"
        b'lastVersion = "0.29.1"\r\ngame = "x"\r\n[filters]\r\nhideCRCs = true\r\n'
    )


def test_s3_bom_y_crlf_se_preservan(tmp_path: pathlib.Path) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_bytes(
        b'\xef\xbb\xbfgame = "x"\r\nuseNoSortingChangesDialog = true # nota\r\n'
        b"enableLootUpdateCheck = false\r\nlastVersion = '0.29.1'\r\n"
    )
    ensure_loot_headless_settings(tmp_path, loot_version=_VERSION)
    assert settings.read_bytes() == (
        b'\xef\xbb\xbfgame = "x"\r\nuseNoSortingChangesDialog = false # nota\r\n'
        b"enableLootUpdateCheck = false\r\nlastVersion = '0.29.1'\r\n"
    )


@pytest.mark.parametrize(
    "contenido",
    [
        b"esto = = no es toml\n",
        b"\xff\xfe no utf-8",
        b'useNoSortingChangesDialog = "false"\n',
        b"useNoSortingChangesDialog.x = 1\n",
        b"enableLootUpdateCheck = 0\n",
        b"lastVersion = 1\n",
        b'lastVersion = """0.29.0"""\n',
    ],
    ids=[
        "toml_invalido",
        "no_utf8",
        "tipo_string",
        "tabla_dotted",
        "entero_no_es_false",
        "version_no_string",
        "multilinea",
    ],
)
def test_s4_settings_no_gestionable_falla_cerrado_sin_pisar(tmp_path: pathlib.Path, contenido: bytes) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_bytes(contenido)
    with pytest.raises(LootHeadlessSettingsError):
        ensure_loot_headless_settings(tmp_path, loot_version=_VERSION)
    assert settings.read_bytes() == contenido
    assert list(tmp_path.glob("*.tmp")) == []


def test_s5_claves_y_valores_exactos_congelados_contra_upstream() -> None:
    """loot_settings.cpp:843-846/854 lee EXACTAMENTE estas claves top-level con
    ``value_or``: cualquier otra grafía o tipo deja el default (modal activo,
    update check activo, lastVersion vacío → First-Time Tips)."""
    assert LOOT_NO_SORTING_CHANGES_DIALOG_KEY == "useNoSortingChangesDialog"
    assert LOOT_UPDATE_CHECK_KEY == "enableLootUpdateCheck"
    assert LOOT_LAST_VERSION_KEY == "lastVersion"
    assert dict(MANAGED_LOOT_HEADLESS_SETTINGS) == {"useNoSortingChangesDialog": False, "enableLootUpdateCheck": False}
    assert (
        frozenset({"useNoSortingChangesDialog", "enableLootUpdateCheck", "lastVersion"}) == MANAGED_LOOT_SETTINGS_KEYS
    )
    assert managed_loot_settings("0.29.2") == {
        "useNoSortingChangesDialog": False,
        "enableLootUpdateCheck": False,
        "lastVersion": "0.29.2",
    }


def test_s5_contenido_generado_parsea_al_valor_gestionado(tmp_path: pathlib.Path) -> None:
    ensure_loot_headless_settings(tmp_path, loot_version=_VERSION)
    datos = tomllib.loads((tmp_path / "settings.toml").read_text(encoding="utf-8"))
    assert datos == managed_loot_settings(_VERSION)


# =============================================================================
# Testigo: armado atómico y dataclasses inmutables
# =============================================================================


def test_armado_reemplaza_log_previo_por_sentinel_con_nonce_unico(tmp_path: pathlib.Path) -> None:
    (tmp_path / LOOT_DEBUG_LOG_FILENAME).write_bytes(b"log de la corrida anterior\n")
    primero = arm_execution_witness(tmp_path)
    segundo = arm_execution_witness(tmp_path)
    assert primero.sentinel != segundo.sentinel
    assert (tmp_path / LOOT_DEBUG_LOG_FILENAME).read_bytes() == segundo.sentinel
    assert segundo.observe().state is LootExecutionWitnessState.SENTINEL_INTACT
    # El sentinel previo ya no está: una observación con el nonce viejo no lo
    # confunde con el nuevo (nunca "intacto" por coincidencia).
    assert primero.observe().state is LootExecutionWitnessState.FRESH


def test_testigo_es_inmutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        TESTIGO_FRESCO.state = LootExecutionWitnessState.ABSENT  # type: ignore[misc]


# =============================================================================
# H1-H6 — hardening post-review de #634
# =============================================================================
# H1: versión REAL del binario (VERSIONINFO) · H2: "First-Time Tips" (lastVersion)
# y update check · H3: finding [[games]] · H4: resultado de worker contradictorio
# · H5: finding de encoding · H6: diagnóstico de TIMEOUT.


def _first_time_tips_se_mostraria(settings_toml: bytes, version_del_binario: str) -> bool:
    """Réplica LITERAL del predicado de loot/loot 0.29.1 ``main_window.cpp:366``
    (0.29.2: l.391): ``getLastVersion() != getLootVersion()``, con el default
    vacío de ``loot_settings.h:131`` y el ``value_or`` de ``loot_settings.cpp:854``
    (un valor que no es string cae al default)."""
    valor = tomllib.loads(settings_toml.decode("utf-8")).get("lastVersion", "")
    return (valor if isinstance(valor, str) else "") != version_del_binario


def _loot_que_lee_sus_settings(capturas: dict[str, bytes]) -> Any:
    """LOOT fake que lee ``settings.toml`` MIENTRAS corre (como ``LootState::init``)."""

    def comportamiento(argv: list[str]) -> None:
        capturas["settings"] = (_data_root(argv) / "settings.toml").read_bytes()
        _loot_que_supera_el_mutex(argv)

    return comportamiento


#: Forma de ``LootSettings::save`` (loot_settings.cpp:961-1063) serializada por
#: toml++ (claves ordenadas, strings literales con comilla simple, rutas Windows
#: sin escapar). Representativa, NO byte-exacta de un rig: el rig real lo confirma.
_SETTINGS_ESCRITO_POR_LOOT = r"""enableDebugLogging = false
enableLootUpdateCheck = false
game = 'Skyrim Special Edition'
language = 'en'
lastGame = 'Skyrim Special Edition'
lastVersion = '0.29.1'
preludeSource = 'https://raw.githubusercontent.com/loot/prelude/v0.26/prelude.yaml'
theme = 'default'
updateMasterlist = true
useNoSortingChangesDialog = false
warnOnCaseSensitiveGamePaths = true

[filters]
hideBashTags = false
hideCRCs = false
hideNotes = false
showOnlyPluginsWithoutLoadOrderMetadata = false

[window]
bottom = 900
left = 100
maximised = false
right = 1500
top = 100

[[games]]
folder = 'Skyrim Special Edition'
gameId = 'SkyrimSE'
hiddenMessages = []
local_path = 'C:\Users\op\AppData\Local\Skyrim Special Edition'
master = 'Skyrim.esm'
masterlistSource = 'https://raw.githubusercontent.com/loot/skyrimse/v0.26/masterlist.yaml'
minimumHeaderVersion = 1.7
name = 'Skyrim Special Edition'
path = 'C:\Games\Steam\steamapps\common\Skyrim Special Edition'

[[languages]]
locale = 'en'
name = 'English'
"""


# --- H1: versión atestiguada desde el recurso VERSIONINFO --------------------


@pytest.mark.parametrize("pe32plus", [True, False], ids=["pe32plus", "pe32"])
@pytest.mark.parametrize(
    ("version", "esperado"),
    [((0, 29, 1), "0.29.1"), ((0, 29, 2), "0.29.2"), ((1, 0, 10), "1.0.10")],
    ids=["0.29.1", "0.29.2", "1.0.10"],
)
def test_h1_version_atestiguada_es_la_de_getlootversion(
    tmp_path: pathlib.Path, pe32plus: bool, version: tuple[int, int, int], esperado: str
) -> None:
    """``FILEVERSION a, b, c, 0`` → ``"a.b.c"`` (resource.rc + set_version_number.py)."""
    exe = tmp_path / "LOOT.exe"
    exe.write_bytes(pe_con_version((*version, 0), pe32plus=pe32plus))
    assert read_loot_binary_version(exe) == esperado


@pytest.mark.parametrize(
    ("contenido", "motivo"),
    [
        (b"", "MZ"),
        (b"loot", "MZ"),
        (b"MZ" + b"\x00" * 126, "firma PE"),
        (pe_con_version(tipo_recurso=3), "ausente"),
        (pe_con_version(idiomas=2), "ambiguo"),
        (pe_con_version(firma=0xDEADBEEF), "firma de VS_FIXEDFILEINFO"),
        (pe_con_version(clave="VS_VERSION_INFX"), "VS_VERSION_INFO"),
        (pe_con_version()[:0x230], "truncad"),
        (pe_con_version((0, 29, 1, 0), (0, 29, 2, 0)), "PRODUCTVERSION"),
        (pe_con_version((0, 29, 1, 7)), "esquema de release"),
    ],
    ids=[
        "vacio",
        "no_pe",
        "mz_sin_pe",
        "sin_rt_version",
        "idiomas_ambiguos",
        "firma_invalida",
        "clave_invalida",
        "truncado",
        "file_distinta_de_product",
        "build_no_cero",
    ],
)
def test_h1_version_no_atestiguable_falla_cerrado(tmp_path: pathlib.Path, contenido: bytes, motivo: str) -> None:
    exe = tmp_path / "LOOT.exe"
    exe.write_bytes(contenido)
    with pytest.raises(LootBinaryVersionError, match=motivo):
        read_loot_binary_version(exe)


def test_h1_binario_ausente_o_ilegible_falla_cerrado(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LootBinaryVersionError, match="No se pudo leer"):
        read_loot_binary_version(tmp_path / "no-existe.exe")
    with pytest.raises(LootBinaryVersionError, match="No se pudo leer"):
        read_loot_binary_version(tmp_path)  # un directorio no es un binario


# --- H2: "First-Time Tips" (lastVersion) y update check ----------------------


async def test_h2_root_nuevo_no_muestra_first_time_tips(tmp_path: pathlib.Path) -> None:
    """Instalación limpia: data root vacío. Sin sembrar, el predicado upstream
    muestra el modal (lastVersion vacío); con la preparación, al momento en que
    LOOT lee sus settings NO hay modal de primer arranque, ni update check, ni
    modal de NO_CHANGE."""
    root = tmp_path / "loot_data"
    root.mkdir()
    assert _first_time_tips_se_mostraria(b"", "0.29.1")  # control: root nuevo sin sembrar
    capturas: dict[str, bytes] = {}
    resultado, _ = await _correr_runner(tmp_path, _loot_que_lee_sus_settings(capturas))
    assert resultado.execution_witness == TESTIGO_FRESCO
    assert not _first_time_tips_se_mostraria(capturas["settings"], "0.29.1")
    datos = tomllib.loads(capturas["settings"].decode("utf-8"))
    assert datos["enableLootUpdateCheck"] is False
    assert datos["useNoSortingChangesDialog"] is False


async def test_h2_actualizar_loot_no_vuelve_a_mostrar_first_time_tips(tmp_path: pathlib.Path) -> None:
    """Upgrade 0.29.1 → 0.29.2 sobre un root que LOOT ya reescribió al cerrar
    (``closeEvent``, main_window.cpp:1489-1490): lastVersion queda en '0.29.1' y
    el binario nuevo mostraría el modal si no se re-siembra."""
    root = tmp_path / "loot_data"
    root.mkdir()
    (root / "settings.toml").write_text(_SETTINGS_ESCRITO_POR_LOOT, encoding="utf-8")
    assert _first_time_tips_se_mostraria(_SETTINGS_ESCRITO_POR_LOOT.encode(), "0.29.2")  # control
    runner = LOOTRunner(LOOTConfig(loot_exe=_loot_exe(tmp_path, (0, 29, 2)), game_path=tmp_path, loot_data_path=root))
    capturas: dict[str, bytes] = {}
    _, fake_exec = _proceso_fake(_loot_que_lee_sus_settings(capturas))
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        await runner.sort()
    assert not _first_time_tips_se_mostraria(capturas["settings"], "0.29.2")


def test_h2_version_igual_es_idempotente_con_el_formato_de_loot(tmp_path: pathlib.Path) -> None:
    """Tras la primera corrida LOOT reescribe el archivo (toml++: strings ``'...'``);
    con las tres claves ya en su valor gestionado no se escribe NADA."""
    settings = tmp_path / "settings.toml"
    settings.write_text(_SETTINGS_ESCRITO_POR_LOOT, encoding="utf-8")
    marca = 2_000_000_000
    os.utime(settings, ns=(marca, marca))
    assert ensure_loot_headless_settings(tmp_path, loot_version="0.29.1") is LootHeadlessSettingsAction.UNCHANGED
    assert settings.read_text(encoding="utf-8") == _SETTINGS_ESCRITO_POR_LOOT
    assert settings.stat().st_mtime_ns == marca


def test_h2_version_distinta_actualiza_exactamente_esa_linea(tmp_path: pathlib.Path) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_text(_SETTINGS_ESCRITO_POR_LOOT, encoding="utf-8")
    assert ensure_loot_headless_settings(tmp_path, loot_version="0.29.2") is LootHeadlessSettingsAction.UPDATED
    antes = _SETTINGS_ESCRITO_POR_LOOT.splitlines(keepends=True)
    despues = settings.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(despues) == len(antes)
    distintas = [(a, d) for a, d in zip(antes, despues, strict=True) if a != d]
    assert distintas == [("lastVersion = '0.29.1'\n", 'lastVersion = "0.29.2"\n')]


def test_h2_toml_ajeno_se_preserva_byte_a_byte_salvo_las_claves_gestionadas(tmp_path: pathlib.Path) -> None:
    """Las tres claves gestionadas con valores ajenos: cambian SÓLO sus líneas;
    rutas Windows en strings literales, floats, tablas y arrays de tablas quedan
    idénticos en bytes y en posición."""
    original = (
        _SETTINGS_ESCRITO_POR_LOOT.replace("enableLootUpdateCheck = false", "enableLootUpdateCheck = true", 1)
        .replace("lastVersion = '0.29.1'", "lastVersion = '0.28.0' # la dejó el GUI", 1)
        .replace("useNoSortingChangesDialog = false", "useNoSortingChangesDialog = true", 1)
    )
    settings = tmp_path / "settings.toml"
    settings.write_text(original, encoding="utf-8")
    assert ensure_loot_headless_settings(tmp_path, loot_version="0.29.1") is LootHeadlessSettingsAction.UPDATED
    texto = settings.read_text(encoding="utf-8")
    antes = original.splitlines(keepends=True)
    despues = texto.splitlines(keepends=True)
    assert len(despues) == len(antes)
    distintas = {a: d for a, d in zip(antes, despues, strict=True) if a != d}
    assert distintas == {
        "enableLootUpdateCheck = true\n": "enableLootUpdateCheck = false\n",
        "lastVersion = '0.28.0' # la dejó el GUI\n": 'lastVersion = "0.29.1" # la dejó el GUI\n',
        "useNoSortingChangesDialog = true\n": "useNoSortingChangesDialog = false\n",
    }
    ajenas_antes = {k: v for k, v in tomllib.loads(original).items() if k not in MANAGED_LOOT_SETTINGS_KEYS}
    ajenas_despues = {k: v for k, v in tomllib.loads(texto).items() if k not in MANAGED_LOOT_SETTINGS_KEYS}
    assert ajenas_despues == ajenas_antes


@pytest.mark.parametrize("version", ["0.29", "v0.29.1", "0.29.1.0", "00.29.1", "0.29.1 ", "\uff10.29.1"])
def test_h2_version_mal_formada_falla_cerrado_sin_tocar_el_archivo(tmp_path: pathlib.Path, version: str) -> None:
    settings = tmp_path / "settings.toml"
    settings.write_text(_SETTINGS_ESCRITO_POR_LOOT, encoding="utf-8")
    with pytest.raises(LootHeadlessSettingsError, match="mal formada"):
        ensure_loot_headless_settings(tmp_path, loot_version=version)
    assert settings.read_text(encoding="utf-8") == _SETTINGS_ESCRITO_POR_LOOT


async def test_h2_version_no_atestiguable_no_lanza_loot_ni_escribe(tmp_path: pathlib.Path) -> None:
    """Fail-closed: sin versión exacta no se siembra nada ni se arma el testigo."""
    root = tmp_path / "loot_data"
    root.mkdir()
    exe = tmp_path / "LOOT" / "LOOT.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"no es un PE")
    runner = LOOTRunner(LOOTConfig(loot_exe=exe, game_path=tmp_path, loot_data_path=root))
    fake_exec = AsyncMock()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
        pytest.raises(LOOTPreconditionError, match="First-Time Tips"),
    ):
        await runner.sort()
    fake_exec.assert_not_awaited()
    assert list(root.iterdir()) == []


async def test_h2_servicio_con_runner_real_reporta_precondicion_y_no_toca_el_load_order(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    tmp_path: pathlib.Path,
) -> None:
    resolver, plugins, loadorder = _load_order(tmp_path)
    exe = tmp_path / "LOOT" / "LOOT.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"no es un PE")
    root = tmp_path / "loot_data"
    runner = LOOTRunner(LOOTConfig(loot_exe=exe, game_path=tmp_path, loot_data_path=root))
    fake_exec = AsyncMock()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        res = await _servicio(lock_manager, snapshot_manager, runner, resolver).sort_load_order()
    fake_exec.assert_not_awaited()
    assert (res["outcome"], res["failure_reason"]) == ("fail", "precondition_failed")
    assert "First-Time Tips" in res["message"]
    assert (plugins.read_bytes(), loadorder.read_bytes()) == (_PLUGINS_ORIGINAL, _LOADORDER_ORIGINAL)


# --- H3: finding de review "clave dentro de [[games]]" ------------------------


def test_h3_clave_solo_dentro_de_games_no_bloquea_ni_se_toca(tmp_path: pathlib.Path) -> None:
    """Finding del revisor automático de #634 REFUTADO: con la clave SÓLO dentro
    de ``[[games]]`` la edición NO falla — antepone la top-level y la de la tabla
    viaja intacta dentro de ``games`` (la validación excluye sólo claves top-level)."""
    original = (
        "enableLootUpdateCheck = false\n"
        "lastVersion = '0.29.1'\n"
        "\n"
        "[[games]]\n"
        "name = 'Skyrim Special Edition'\n"
        "useNoSortingChangesDialog = true\n"
    )
    settings = tmp_path / "settings.toml"
    settings.write_text(original, encoding="utf-8")
    assert ensure_loot_headless_settings(tmp_path, loot_version="0.29.1") is LootHeadlessSettingsAction.UPDATED
    texto = settings.read_text(encoding="utf-8")
    assert texto == "useNoSortingChangesDialog = false\n" + original
    datos = tomllib.loads(texto)
    assert datos["useNoSortingChangesDialog"] is False
    assert datos["games"] == [{"name": "Skyrim Special Edition", "useNoSortingChangesDialog": True}]


def test_h3_la_validacion_si_rechaza_un_cambio_dentro_de_games() -> None:
    """Contracara: la exclusión es SÓLO top-level. Una edición que tocara la clave
    homónima de ``[[games]]`` cambia el valor de ``games`` y se rechaza."""
    from sky_claw.local.loot import headless_settings as hs

    antes = tomllib.loads("[[games]]\nuseNoSortingChangesDialog = true\n")
    manipulado = (
        "useNoSortingChangesDialog = false\nenableLootUpdateCheck = false\n"
        'lastVersion = "0.29.1"\n[[games]]\nuseNoSortingChangesDialog = false\n'
    )
    with pytest.raises(LootHeadlessSettingsError, match="sólo las claves gestionadas"):
        hs._validar_edicion(manipulado, antes=antes, deseado=managed_loot_settings("0.29.1"))


# --- H4: resultado de worker contradictorio → PROTOCOL_ERROR ------------------


async def test_h4_error_de_protocolo_revierte_y_se_reporta_tipado(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    journal,  # noqa: ANN001
    tmp_path: pathlib.Path,
) -> None:
    """LOOT pudo haber mutado antes de que el worker respondiera algo fuera de
    contrato: ningún campo es evidencia → FAIL/protocol_error y rollback."""
    from sky_claw.app.db.journal import TransactionStatus

    resolver, plugins, _ = _load_order(tmp_path)

    async def sort(**_kwargs: object) -> LOOTResult:
        plugins.write_bytes(b"*Skyrim.esm\nMod B.esp\n*Mod A.esp\n")
        raise LOOTWorkerProtocolError("Resultado del worker LOOT contradictorio: ...")

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort)
    res = await _servicio(lock_manager, snapshot_manager, runner, resolver, journal).sort_load_order()
    assert (res["outcome"], res["success"], res["failure_reason"]) == ("fail", False, "protocol_error")
    assert res["rolled_back"] is True
    assert plugins.read_bytes() == _PLUGINS_ORIGINAL
    assert (await _estado_ultima_tx(journal)).status == TransactionStatus.ROLLED_BACK


async def test_h4_camino_brokered_completo_kind_con_success_true(
    lock_manager,  # noqa: ANN001
    snapshot_manager,  # noqa: ANN001
    tmp_path: pathlib.Path,
) -> None:
    """Extremo a extremo por el runner productivo: el finding pedía que esto NO
    terminara como PROCESS_ERROR genérico."""
    resolver, _, _ = _load_order(tmp_path)
    broker = _BrokerConResultado(success=True, tool_result={"loot_failure_kind": "timeout"})
    runner = _runner_brokered(tmp_path, broker)
    res = await _servicio(lock_manager, snapshot_manager, runner, resolver).sort_load_order()
    assert (res["outcome"], res["failure_reason"]) == ("fail", "protocol_error")
    assert "contradictorio" in res["message"]


# --- H5: finding de encoding (Windows-1252) -----------------------------------

#: Bytes que el ``cp1252`` de Python no mapea y WHATWG/encoding_rs mapea a C1.
_SIN_MAPEO_EN_CP1252_DE_PYTHON = frozenset({0x81, 0x8D, 0x8F, 0x90, 0x9D})


def _decodificar_como_libloadorder(data: bytes) -> str:
    """``encoding_rs::WINDOWS_1252`` sin BOM ni reemplazo: total e inyectivo."""
    return "".join(chr(b) if b in _SIN_MAPEO_EN_CP1252_DE_PYTHON else bytes([b]).decode("cp1252") for b in data)


def _lineas_como_rust(texto: str) -> list[str]:
    """``str::lines()`` = ``split_inclusive('\\n')`` + ``LinesMap`` (Rust 1.82)."""
    lineas: list[str] = []
    resto = texto
    while resto:
        corte = resto.find("\n")
        pieza, resto = (resto, "") if corte < 0 else (resto[: corte + 1], resto[corte + 1 :])
        if pieza.endswith("\n"):
            pieza = pieza[:-1]
            if pieza.endswith("\r"):
                pieza = pieza[:-1]
        lineas.append(pieza)
    return lineas


def _semantica_de_referencia(data: bytes) -> tuple[tuple[str, bool], ...]:
    """Réplica TEXTUAL de libloadorder 18.8.1 (mutable.rs:328-332 +
    asterisk_based.rs:314-322): decodificar, ``lines()``, descartar vacías y
    ``#``, ``*`` = activo."""
    entradas: list[tuple[str, bool]] = []
    for linea in _lineas_como_rust(_decodificar_como_libloadorder(data)):
        if not linea or linea.startswith("#"):
            continue
        entradas.append((linea[1:], True) if linea.startswith("*") else (linea, False))
    return tuple(entradas)


_CORPUS_ENCODING: tuple[bytes, ...] = (
    b"*Skyrim.esm\nMod A.esp\n",
    b"*Skyrim.esm\r\nMod A.esp\r\n",
    b"*Skyrim.esm\nMod A.esp",
    b"*Skyrim.esm\nMod A.esp\r",
    b"\xef\xbb\xbf*Skyrim.esm\nMod A.esp\n",
    "*Skyrim.esm\nCafé.esp\n".encode(),
    "*Skyrim.esm\nCafé.esp\n".encode("cp1252"),
    "*Skyrim.esm\n\U0001f600 Mod.esp\n".encode(),
    "*Skyrim.esm\n*\U0001f600 Mod.esp\n".encode(),
    "*Skyrim.esm\nМод.esp\n".encode(),
    b"*Skyrim.esm\n\x81\x8d\x8f\x90\x9d.esp\n",
    b"*Skyrim.esm\n\xc2\x81.esp\n",
    b"# MO2\n*Skyrim.esm\n\nMod A.esp\n",
    b"*Skyrim.esm\nmod a.esp\n",
    b"*Skyrim.esm\n\r\nMod A.esp\n",
    b"\r",
    b"",
)


def test_h5_comparar_bytes_equivale_a_comparar_lo_que_lee_libloadorder() -> None:
    """Finding de encoding (warning, no bug): para TODO par del corpus — UTF-8,
    emoji, cirílico, cp1252, los 5 bytes "sin uso", BOM, CRLF, CR final — la
    igualdad del parser por bytes coincide con la del texto que decodifica
    libloadorder. Nunca "cambio" donde libloadorder ve igualdad ni al revés."""
    for a, b in itertools.product(_CORPUS_ENCODING, repeat=2):
        assert (parse_load_order_bytes(a) == parse_load_order_bytes(b)) == (
            _semantica_de_referencia(a) == _semantica_de_referencia(b)
        ), (a, b)
    # Más fuerte que la igualdad por pares: decodificar cada nombre del parser por
    # bytes da EXACTAMENTE la entrada que lee libloadorder.
    for dato in _CORPUS_ENCODING:
        decodificado = tuple((_decodificar_como_libloadorder(n), activo) for n, activo in parse_load_order_bytes(dato))
        assert decodificado == _semantica_de_referencia(dato), dato


def test_h5_nombres_fuera_de_windows_1252_sin_cambio_son_no_change() -> None:
    utf8 = "*Skyrim.esm\n*\U0001f600 Mod.esp\nМод.esp\n".encode()
    estado = ((str(pathlib.Path("plugins.txt")), parse_load_order_bytes(utf8)),)
    veredicto = classify_loot_sort(
        process_success=True, process_detail="", witness=TESTIGO_FRESCO, before=estado, after=estado
    )
    assert veredicto.outcome is LootSortOutcome.NO_CHANGE
    reordenado = (
        (
            str(pathlib.Path("plugins.txt")),
            parse_load_order_bytes("*Skyrim.esm\nМод.esp\n*\U0001f600 Mod.esp\n".encode()),
        ),
    )
    veredicto = classify_loot_sort(
        process_success=True, process_detail="", witness=TESTIGO_FRESCO, before=estado, after=reordenado
    )
    assert veredicto.outcome is LootSortOutcome.CHANGED


# --- H6: el diagnóstico de TIMEOUT ya no culpa a lo que los settings neutralizan


async def test_h6_timeout_ya_no_lista_first_time_tips(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    resolver, _, _ = _load_order(tmp_path)
    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=LOOTTimeoutError(120))
    res = await _servicio(lock_manager, snapshot_manager, runner, resolver).sort_load_order()
    assert "First-Time Tips" not in res["outcome_detail"]
    assert "masterlist" in res["outcome_detail"]  # PR-3: sigue siendo una causa posible
