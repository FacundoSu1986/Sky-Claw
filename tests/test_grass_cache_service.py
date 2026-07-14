"""Tests del ``GrassCacheService`` (PR-5 del plan grass cache).

Orquesta las Fases A→D del Stage 8 (NGIO) componiendo las piezas ya mergeadas
(``GrassAnalyzer`` PR-2, ``GrassProfileManager`` PR-3, ``GrassCacheRunner``
PR-4) bajo el lock distribuido, con journal por fase y eventos lifecycle.

Anclas del contrato:
- Todo retorno cumple ``success: bool`` + ``message: str`` (vacío en éxito;
  ``normalize_tool_result`` jamás cae en "error desconocido") — patrón
  ``test_tool_result_contract.py``.
- Guard Stage 5→8 NUEVO (§5.2 del SOP): sin constancia en el journal de que
  LOOT (Stage 5) completó, ``generate`` rechaza sin mutar nada.
- El lock cubre el ritual entero; ``teardown()`` corre SIEMPRE (éxito, fallo
  del runner, cancelación); el cache parcial se conserva (nunca se borra
  ``overwrite/Grass``).
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.db.journal import OperationStatus
from sky_claw.antigravity.db.locks import LockAcquisitionError
from sky_claw.local.mo2.grass_profile import GrassProfileError
from sky_claw.local.tools.grass_cache_runner import GrassCacheRunResult
from sky_claw.local.tools.grass_cache_service import (
    GrassCacheService,
)
from sky_claw.local.tools.tool_result import normalize_tool_result
from sky_claw.local.xedit.grass_analyzer import (
    GrassWorldspace,
    GrassWorldspaceReport,
    ZeroBoundGrass,
    ZeroBoundReport,
)


def _assert_error_de_contrato(result: dict[str, Any], fragmento: str) -> None:
    """El shape de error canónico: success False + message accionable."""
    assert result["success"] is False
    assert fragmento.lower() in result["message"].lower()
    assert normalize_tool_result(result)["message"] != "error desconocido"


def _resultado_runner(**overrides: Any) -> GrassCacheRunResult:
    base: dict[str, Any] = {
        "success": True,
        "message": "",
        "outcome": "completed",
        "crash_count": 3,
        "cgid_count": 120,
        "cache_size_mb": 45.5,
        "elapsed_s": 3600.0,
    }
    base.update(overrides)
    return GrassCacheRunResult(**base)


def _journal_con_loot_completado() -> AsyncMock:
    """Journal fake donde Stage 5 (LOOT) consta como completado."""
    journal = AsyncMock()
    journal.get_last_operation.return_value = MagicMock(status=OperationStatus.COMPLETED)
    journal.begin_transaction.return_value = 77
    journal.begin_operation.return_value = 5
    return journal


def _lock_tx_fake() -> MagicMock:
    """SnapshotTransactionLock fake: async CM que no hace nada."""
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    tx.rollback_completed = False
    return tx


def _servicio(
    tmp_path: pathlib.Path,
    *,
    journal: Any = None,
    runner_result: GrassCacheRunResult | None = None,
    lock_tx: Any = None,
) -> tuple[GrassCacheService, dict[str, Any]]:
    """Servicio con TODAS las dependencias inyectadas y trackeadas."""
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    mo2_root = tmp_path / "mo2"
    (mo2_root / "overwrite").mkdir(parents=True, exist_ok=True)

    profile_manager = AsyncMock()
    profile_manager.clone_profile = "SkyClaw-GrassCache"

    runner = AsyncMock()
    runner.run.return_value = runner_result if runner_result is not None else _resultado_runner()
    runner_factory = MagicMock(return_value=runner)

    event_bus = AsyncMock()
    colaboradores = {
        "profile_manager": profile_manager,
        "runner": runner,
        "runner_factory": runner_factory,
        "event_bus": event_bus,
    }
    service = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=journal,
        event_bus=event_bus,
        profile_manager=profile_manager,
        mo2=MagicMock(),
        game_path=game,
        overwrite_grass_dir=mo2_root / "overwrite" / "Grass",
        runner_factory=runner_factory,
        lock_factory=(lambda **_kw: lock_tx) if lock_tx is not None else (lambda **_kw: _lock_tx_fake()),
    )
    return service, colaboradores


_PAYLOAD = {"worldspaces": ["Tamriel", "DLC2SolstheimWorld"], "conflicting_mods": ["ENB Helper"]}


# ---------------------------------------------------------------------------
# analyze_prerequisites (Fase A — read-only, sin lock)
# ---------------------------------------------------------------------------


async def test_analyze_reporta_worldspaces_y_bounds(tmp_path: pathlib.Path) -> None:
    service, _ = _servicio(tmp_path)
    analyzer = AsyncMock()
    analyzer.list_grass_worldspaces.return_value = GrassWorldspaceReport(
        worldspaces=[GrassWorldspace(form_id="0000003C", editor_id="Tamriel", plugin="Skyrim.esm")],
        summary={"grass_worldspaces": 1, "land_scanned": 100, "ltex_grass": 5},
    )
    analyzer.detect_zero_bound_grass.return_value = ZeroBoundReport(
        findings=[], summary={"total_gras": 10, "zero_bounds": 0}
    )
    service._analyzer = analyzer
    service._xedit_runner_provider = lambda: MagicMock()

    resultado = await service.analyze_prerequisites(["Skyrim.esm"])

    assert resultado["success"] is True
    assert resultado["message"] == ""
    assert resultado["editor_ids"] == ["Tamriel"]
    assert resultado["ready"] is True


async def test_analyze_con_zero_bounds_no_esta_ready(tmp_path: pathlib.Path) -> None:
    service, _ = _servicio(tmp_path)
    analyzer = AsyncMock()
    analyzer.list_grass_worldspaces.return_value = GrassWorldspaceReport(
        worldspaces=[GrassWorldspace(form_id="0000003C", editor_id="Tamriel", plugin="Skyrim.esm")],
        summary={"grass_worldspaces": 1, "land_scanned": 100, "ltex_grass": 5},
    )
    analyzer.detect_zero_bound_grass.return_value = ZeroBoundReport(
        findings=[
            ZeroBoundGrass(
                form_id="0101A001",
                editor_id="Rota",
                winner_plugin="Broken.esp",
                origin_plugin="Broken.esp",
                reason="zeros",
            )
        ],
        summary={"total_gras": 10, "zero_bounds": 1},
    )
    service._analyzer = analyzer
    service._xedit_runner_provider = lambda: MagicMock()

    resultado = await service.analyze_prerequisites(["Broken.esp"])

    assert resultado["success"] is True
    assert resultado["ready"] is False, "hallazgos de zero-bounds bloquean el ready"
    assert resultado["zero_bounds"]["findings"][0]["winner_plugin"] == "Broken.esp"


async def test_analyze_fallo_de_xedit_cumple_contrato(tmp_path: pathlib.Path) -> None:
    service, _ = _servicio(tmp_path)
    analyzer = AsyncMock()
    analyzer.list_grass_worldspaces.side_effect = RuntimeError("El análisis de xEdit falló (exit code 1).")
    service._analyzer = analyzer
    service._xedit_runner_provider = lambda: MagicMock()

    resultado = await service.analyze_prerequisites(["Skyrim.esm"])

    _assert_error_de_contrato(resultado, "xEdit")


async def test_analyze_sin_runner_configurado_cumple_contrato(tmp_path: pathlib.Path) -> None:
    service, _ = _servicio(tmp_path)  # sin _xedit_runner_provider

    resultado = await service.analyze_prerequisites(["Skyrim.esm"])

    _assert_error_de_contrato(resultado, "xEdit")


# ---------------------------------------------------------------------------
# generate — guard Stage 5→8 (nuevo; §5.2 del SOP)
# ---------------------------------------------------------------------------


async def test_generate_sin_loot_completado_rechaza_sin_mutar(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    journal.get_last_operation.return_value = None  # LOOT jamás corrió
    service, colab = _servicio(tmp_path, journal=journal)

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "LOOT")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()
    colab["runner_factory"].assert_not_called()


async def test_generate_sin_journal_es_fail_closed(tmp_path: pathlib.Path) -> None:
    service, colab = _servicio(tmp_path, journal=None)

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "journal")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()


async def test_generate_force_stage_guard_saltea_el_guard(tmp_path: pathlib.Path) -> None:
    service, _ = _servicio(tmp_path, journal=None)

    resultado = await service.generate({**_PAYLOAD, "force_stage_guard": True})

    assert resultado["success"] is True, resultado["message"]


# ---------------------------------------------------------------------------
# generate — orquestación B→D
# ---------------------------------------------------------------------------


async def test_generate_happy_path_completo(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True
    assert resultado["message"] == ""
    assert resultado["outcome"] == "completed"
    assert resultado["cgid_count"] == 120
    # Fase B completa, en orden, sobre el clon.
    pm = colab["profile_manager"]
    pm.create_clone_profile.assert_awaited_once()
    pm.build_config_mod.assert_awaited_once()
    assert pm.build_config_mod.await_args.args[0] == ["Tamriel", "DLC2SolstheimWorld"]
    pm.disable_conflicting_mods.assert_awaited_once_with(["ENB Helper"])
    # Fase D: teardown SIEMPRE (el entorno se restaura también en éxito).
    pm.teardown.assert_awaited()
    # Journal: transacción committeada.
    journal.begin_transaction.assert_awaited_once()
    journal.commit_transaction.assert_awaited_once_with(77)
    # Eventos lifecycle.
    topics = [c.args[0].topic for c in colab["event_bus"].publish.await_args_list]
    assert "pipeline.grass_cache.started" in topics
    assert "pipeline.grass_cache.completed" in topics


async def test_generate_payload_invalido_cumple_contrato(tmp_path: pathlib.Path) -> None:
    service, colab = _servicio(tmp_path, journal=_journal_con_loot_completado())

    resultado = await service.generate({"worldspaces": []})

    _assert_error_de_contrato(resultado, "worldspaces")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()


async def test_generate_lock_ocupado_cumple_contrato(tmp_path: pathlib.Path) -> None:
    tx = _lock_tx_fake()
    tx.__aenter__ = AsyncMock(side_effect=LockAcquisitionError("grass-cache", "otro-agente"))
    service, colab = _servicio(tmp_path, journal=_journal_con_loot_completado(), lock_tx=tx)

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "lock")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()


async def test_generate_fallo_de_fase_b_hace_teardown_y_cumple_contrato(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    colab["profile_manager"].build_config_mod.side_effect = GrassProfileError("el clon ya existe")

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "clon")
    colab["profile_manager"].teardown.assert_awaited(), "rollback de Fase B = teardown"
    colab["runner_factory"].assert_not_called()
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)


async def test_generate_runner_fallido_conserva_cache_y_reporta(tmp_path: pathlib.Path) -> None:
    fallo = _resultado_runner(
        success=False,
        message="5 relanzamientos consecutivos sin ningún .cgid nuevo",
        outcome="stalled",
        stalled=True,
        cgid_count=12,
    )
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal, runner_result=fallo)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is False
    assert resultado["outcome"] == "stalled"
    assert "sin ningún .cgid" in resultado["message"]
    assert normalize_tool_result(resultado)["message"] != "error desconocido"
    colab["profile_manager"].teardown.assert_awaited()
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)


async def test_generate_cancelacion_hace_teardown_y_propaga(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    colab["runner"].run.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.generate(_PAYLOAD)

    colab["profile_manager"].teardown.assert_awaited()


async def test_generate_progreso_del_runner_se_publica_al_bus(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)

    await service.generate(_PAYLOAD)

    # El runner recibió un on_progress inyectado (traducción callback→bus).
    kwargs = colab["runner_factory"].call_args.kwargs
    assert kwargs.get("on_progress") is not None
    from sky_claw.local.tools.grass_cache_runner import GrassCacheProgress

    await kwargs["on_progress"](
        GrassCacheProgress(phase="scanning", crash_count=1, cgid_count=5, cache_size_mb=1.0, elapsed_s=60.0)
    )
    topics = [c.args[0].topic for c in colab["event_bus"].publish.await_args_list]
    assert "pipeline.grass_cache.progress" in topics
