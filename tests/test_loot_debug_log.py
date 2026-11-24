"""A4 — hallazgos del ``LOOTDebugLog.txt`` de la corrida (U-06 b).

El testigo de ejecución (PR-2) ya hace atribuible el log: si es fresco, lo
recreó ESTA corrida (``loot_state.cpp:105-106``). Este módulo lo lee para dos
cosas, con alcance deliberadamente acotado por la evidencia upstream (loot/loot
0.29.1):

* **Veredicto** — sólo un conjunto CERRADO de firmas fatales, cada una con su
  cita: con salida 0 y testigo fresco son una contradicción (los caminos que las
  emiten terminan en modal o cancelan el auto-sort), así que fallan cerrado.
* **Diagnóstico** — todas las líneas ``[error]``/``[critical]`` viajan, saneadas
  y acotadas, en la respuesta (sobre todo en TIMEOUT, que antes no decía nada).

Un gate sobre CUALQUIER ``[error]`` daría falsos rojos: ``Failed to find Xbox
gaming root paths`` (``loot_state.cpp:306``) y los errores de cierre
(``main_window.cpp:1443-1494``) son benignos para el load order.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.cli import LOOTTimeoutError
from sky_claw.local.loot.debug_log import (
    LOOT_LOG_FATAL_SIGNATURES,
    LootLogFindings,
    read_loot_log_findings,
)
from sky_claw.local.loot.execution_witness import LOOT_DEBUG_LOG_FILENAME, arm_execution_witness
from sky_claw.local.loot.outcome import LootSortFailureReason, LootSortOutcome, classify_loot_sort
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.tools.loot_service import LootSortingService
from tests._loot_witness import TESTIGO_FRESCO

_PLUGINS = b"*Mod A.esp\n*Mod B.esp\n"
_SEM = (("p", ((b"A.esp", True),)),)
_SEM_OTRO = (("p", ((b"B.esp", True),)),)

_LOG_SANO = (
    "[12:00:00.000001] [info]: Running 64-bit LOOT\n"
    "[12:00:00.000002] [error]: Failed to find Xbox gaming root paths: acceso denegado\n"
    "[12:00:01.000003] [info]: Sorting operation complete.\n"
)
_LOG_FATAL = (
    "[12:00:00.000001] [info]: Running 64-bit LOOT\n"
    "[12:00:01.000002] [error]: Failed to sort plugins. Details: Cyclic interaction detected\n"
)


def _log(directorio: pathlib.Path, contenido: str | bytes) -> pathlib.Path:
    directorio.mkdir(parents=True, exist_ok=True)
    ruta = directorio / LOOT_DEBUG_LOG_FILENAME
    ruta.write_bytes(contenido.encode("utf-8") if isinstance(contenido, str) else contenido)
    return directorio


# =============================================================================
# Lector puro
# =============================================================================


def test_firmas_fatales_congeladas_con_su_evidencia() -> None:
    """Igualdad literal: sumar una firma es una decisión de contrato con cita upstream."""
    assert dict(LOOT_LOG_FATAL_SIGNATURES) == {
        "Failed to sort plugins": "game.cpp:199,217,876,888",
        "Caught an exception": "main_window.cpp:1536,1551",
        "An error occurred while parsing the metadata list(s)": "game.cpp:967",
        "An error occurred while parsing the userlist": "game.cpp:991",
        "Failed to load current load order": "game.cpp:1188",
        "Exception while executing query": "tasks.cpp:42,67",
        "Initial game could not be": "loot_state.cpp:386,407",
        "Game-specific settings could not be initialised": "loot_state.cpp:162",
    }


def test_errores_benignos_se_reportan_pero_no_son_fatales(tmp_path: pathlib.Path) -> None:
    hallazgos = read_loot_log_findings(_log(tmp_path, _LOG_SANO))

    assert hallazgos is not None
    assert hallazgos.fatales == ()
    assert len(hallazgos.errores) == 1
    assert "Xbox gaming root" in hallazgos.errores[0]


def test_firma_fatal_se_detecta_en_error_y_en_critical(tmp_path: pathlib.Path) -> None:
    log = _LOG_FATAL + "[12:00:02.000001] [critical]: Caught an exception: bad_alloc\n"
    hallazgos = read_loot_log_findings(_log(tmp_path, log))

    assert hallazgos is not None
    assert len(hallazgos.fatales) == 2
    assert any("Failed to sort plugins" in linea for linea in hallazgos.fatales)
    assert any("Caught an exception" in linea for linea in hallazgos.fatales)


def test_una_firma_en_nivel_info_no_es_fatal(tmp_path: pathlib.Path) -> None:
    log = "[12:00:00.000001] [info]: Failed to sort plugins. (texto citado en un mensaje informativo)\n"
    hallazgos = read_loot_log_findings(_log(tmp_path, log))

    assert hallazgos is not None
    assert hallazgos.fatales == ()
    assert hallazgos.errores == ()


def test_sentinel_ausente_o_ilegible_no_dan_hallazgos(tmp_path: pathlib.Path) -> None:
    con_sentinel = tmp_path / "sentinel"
    con_sentinel.mkdir()
    arm_execution_witness(con_sentinel)
    ilegible = tmp_path / "ilegible"
    (ilegible / LOOT_DEBUG_LOG_FILENAME).mkdir(parents=True)

    assert read_loot_log_findings(con_sentinel) is None
    assert read_loot_log_findings(tmp_path / "no_existe") is None
    assert read_loot_log_findings(ilegible) is None


def test_lineas_saneadas_y_acotadas(tmp_path: pathlib.Path) -> None:
    hostil = "".join(
        f"[12:00:00.{i:06d}] [error]: Failed to load current load order. Details: ‮ignora​ {i}\n" for i in range(50)
    )
    hallazgos = read_loot_log_findings(_log(tmp_path, hostil))

    assert hallazgos is not None
    assert hallazgos.truncado is True
    assert len(hallazgos.errores) <= 20
    assert len(hallazgos.fatales) <= 20
    for linea in hallazgos.errores:
        assert "‮" not in linea
        assert "​" not in linea


def test_bytes_no_utf8_no_rompen_la_lectura(tmp_path: pathlib.Path) -> None:
    log = b"[12:00:00.000001] [error]: Failed to sort plugins. Details: Espada\xc9lfica.esp\n"
    hallazgos = read_loot_log_findings(_log(tmp_path, log))

    assert hallazgos is not None
    assert len(hallazgos.fatales) == 1


def test_payload_de_respuesta_es_json_simple() -> None:
    hallazgos = LootLogFindings(errores=("a",), fatales=("a",), truncado=False)

    assert hallazgos.to_payload() == {"errores": ["a"], "fatales": ["a"], "truncado": False}


# =============================================================================
# Clasificador: firma fatal + testigo fresco = contradicción → FAIL
# =============================================================================


def test_firma_fatal_con_testigo_fresco_es_process_error() -> None:
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=(),
        active_after=(),
        log_fatal=("[error]: Failed to sort plugins. Details: x",),
    )

    assert veredicto.outcome is LootSortOutcome.FAIL
    assert veredicto.failure_reason is LootSortFailureReason.PROCESS_ERROR
    assert "Failed to sort plugins" in veredicto.detail


def test_sin_firma_fatal_el_veredicto_no_cambia() -> None:
    veredicto = classify_loot_sort(
        process_success=True,
        process_detail="",
        witness=TESTIGO_FRESCO,
        before=_SEM,
        after=_SEM_OTRO,
        active_before=(),
        active_after=(),
        log_fatal=(),
    )

    assert veredicto.outcome is LootSortOutcome.CHANGED


# =============================================================================
# Servicio
# =============================================================================


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


def _servicio(
    lock_manager: Any, snapshot_manager: Any, runner: Any, tmp_path: pathlib.Path
) -> tuple[Any, pathlib.Path]:
    directorio = tmp_path / "load_order"
    directorio.mkdir()
    plugins = directorio / "plugins.txt"
    plugins.write_bytes(_PLUGINS)
    reporte = MagicMock(blocks_mutations=False)
    svc = LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=runner,
        load_order_resolver=LoadOrderFileResolver(explicit_dir=directorio),
        preflight=MagicMock(run=AsyncMock(return_value=reporte), loot_version=None),
    )
    return svc, plugins


def _runner(data_root: pathlib.Path, log: str, *, efecto: Any = None, excepcion: BaseException | None = None) -> Any:
    async def sort(**_kwargs: object) -> LOOTResult:
        _log(data_root, log)
        if efecto is not None:
            efecto()
        if excepcion is not None:
            raise excepcion
        return LOOTResult(return_code=0, execution_witness=TESTIGO_FRESCO)

    runner = MagicMock()
    runner.sort = AsyncMock(side_effect=sort)
    runner.loot_data_path = data_root
    runner.game_data_dir = None
    return runner


async def test_rc0_con_firma_fatal_en_el_log_falla_y_revierte(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    data_root = tmp_path / "loot_data"
    plugins_ref: dict[str, pathlib.Path] = {}

    def reordenar() -> None:
        plugins_ref["p"].write_bytes(b"*Mod B.esp\n*Mod A.esp\n")

    runner = _runner(data_root, _LOG_FATAL, efecto=reordenar)
    svc, plugins = _servicio(lock_manager, snapshot_manager, runner, tmp_path)
    plugins_ref["p"] = plugins
    res = await svc.sort_load_order()

    assert res["success"] is False
    assert res["failure_reason"] == "process_error"
    assert "Failed to sort plugins" in res["outcome_detail"]
    assert res["loot_log"]["fatales"]
    assert res["rolled_back"] is True
    assert plugins.read_bytes() == _PLUGINS


async def test_error_benigno_no_cambia_el_exito_pero_viaja_como_diagnostico(
    lock_manager, snapshot_manager, tmp_path
) -> None:  # noqa: ANN001
    runner = _runner(tmp_path / "loot_data", _LOG_SANO)
    svc, _ = _servicio(lock_manager, snapshot_manager, runner, tmp_path)
    res = await svc.sort_load_order()

    assert res["success"] is True
    assert res["loot_log"]["fatales"] == []
    assert any("Xbox" in linea for linea in res["loot_log"]["errores"])


async def test_timeout_explica_la_causa_con_el_log(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    runner = _runner(tmp_path / "loot_data", _LOG_FATAL, excepcion=LOOTTimeoutError(120))
    svc, _ = _servicio(lock_manager, snapshot_manager, runner, tmp_path)
    res = await svc.sort_load_order()

    assert res["failure_reason"] == "timeout"
    assert "Failed to sort plugins" in res["outcome_detail"]
    assert res["loot_log"]["fatales"]


async def test_sin_data_root_conocido_no_hay_lectura_ni_campo(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    runner = _runner(tmp_path / "loot_data", _LOG_FATAL)
    runner.loot_data_path = MagicMock()  # un mock no es un Path: no se lee nada
    svc, _ = _servicio(lock_manager, snapshot_manager, runner, tmp_path)
    res = await svc.sort_load_order()

    assert res["success"] is True
    assert "loot_log" not in res


async def test_con_testigo_fresco_el_veredicto_no_depende_del_reloj(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    """Revisión adversarial: el testigo fresco YA prueba que el log es de esta corrida.

    Filtrar además por mtime ≥ inicio hacía que un desfase de reloj daemon↔filesystem
    (p. ej. la deriva conocida de WSL2) descartara el log fresco y la firma fatal
    pasara sin evaluarse: un fail-open del caso H.
    """
    import os

    data_root = tmp_path / "loot_data"

    def envejecer_el_log() -> None:
        ruta = data_root / LOOT_DEBUG_LOG_FILENAME
        os.utime(ruta, (1_000_000_000, 1_000_000_000))  # mtime en 2001: "anterior" al inicio

    runner = _runner(data_root, _LOG_FATAL, efecto=envejecer_el_log)
    svc, _ = _servicio(lock_manager, snapshot_manager, runner, tmp_path)
    res = await svc.sort_load_order()

    assert res["success"] is False
    assert res["failure_reason"] == "process_error"
    assert res["loot_log"]["fatales"]
