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

from sky_claw.app.db.journal import OperationStatus, TransactionStatus
from sky_claw.app.db.journal_contracts import GRASS_VERDICT_KIND, is_teardown_incomplete
from sky_claw.app.db.locks import LockAcquisitionError
from sky_claw.local.mo2.grass_profile import GrassProfileError
from sky_claw.local.tools.grass_cache_runner import GrassCacheRunResult
from sky_claw.local.tools.grass_cache_service import (
    GenerateGrassCacheParams,
    GrassCacheService,
    GrassRuntimeDeps,
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
    """Journal fake donde Stage 5 (LOOT) consta como completado Y commiteado.

    El marcador confiable es el FlightReport post-éxito (kind="flight_report",
    transaction_status="committed"): el ActionManifest pre-sort también queda
    COMPLETED aunque el sort falle (review Codex #291), así que una entry
    COMPLETED pelada NO alcanza.
    """
    journal = AsyncMock()
    journal.get_last_operation.return_value = MagicMock(
        status=OperationStatus.COMPLETED,
        metadata={"kind": "flight_report", "transaction_status": "committed"},
    )
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
    lock_txs: dict[str, Any] | None = None,
) -> tuple[GrassCacheService, dict[str, Any]]:
    """Servicio con TODAS las dependencias inyectadas y trackeadas.

    ``lock_tx`` inyecta el tx del recurso ``grass-cache``; ``lock_txs`` permite
    inyectar por recurso (p.ej. ``load-order``). La fábrica registra cada
    pedido de lock en ``colaboradores["llamadas_lock"]``.
    """
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    mo2_root = tmp_path / "mo2"
    (mo2_root / "overwrite").mkdir(parents=True, exist_ok=True)

    profile_manager = AsyncMock()
    profile_manager.clone_profile = "SkyClaw-GrassCache"
    # El clon (LANZAMIENTO) y el perfil de ORIGEN (identidad del progreso)
    # son nombres distintos: el mock declara los dos.
    profile_manager.source_profile = "Default"
    # teardown() ahora devuelve la lista de paths que no pudo borrar (§1.6).
    profile_manager.teardown.return_value = []

    runner = AsyncMock()
    runner.run.return_value = runner_result if runner_result is not None else _resultado_runner()
    runner_factory = MagicMock(return_value=runner)

    llamadas_lock: list[dict[str, Any]] = []

    def _fabrica_lock(**kwargs: Any) -> Any:
        llamadas_lock.append(kwargs)
        recurso = kwargs.get("resource_id")
        if lock_txs is not None and recurso in lock_txs:
            return lock_txs[recurso]
        if lock_tx is not None and recurso == "grass-cache":
            return lock_tx
        return _lock_tx_fake()

    event_bus = AsyncMock()
    colaboradores = {
        "profile_manager": profile_manager,
        "runner": runner,
        "runner_factory": runner_factory,
        "event_bus": event_bus,
        "llamadas_lock": llamadas_lock,
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
        lock_factory=_fabrica_lock,
    )
    return service, colaboradores


_PAYLOAD = {
    "worldspaces": ["Tamriel", "DLC2SolstheimWorld"],
    "conflicting_mods": ["ENB Helper"],
    # La cobertura USVFS no está demostrada (P0): el operador lo reconoce. Los
    # tests del HOLD lo apagan explícitamente.
    "ack_usvfs_no_verificado": True,
}


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


async def test_generate_guard_rechaza_manifiesto_sin_flight_report(tmp_path: pathlib.Path) -> None:
    """Un ActionManifest pre-sort queda COMPLETED aunque el sort falle (la
    transacción se marca rolled-back, la operación no): el guard NO debe
    aceptarlo como prueba de Stage 5 — exige el FlightReport post-éxito
    (review Codex #291)."""
    journal = _journal_con_loot_completado()
    journal.get_last_operation.return_value = MagicMock(
        status=OperationStatus.COMPLETED,
        metadata={"ritual_id": "loot-sort-99", "tool": "LOOT"},  # manifiesto, sin kind
    )
    service, colab = _servicio(tmp_path, journal=journal)

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "LOOT")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()


async def test_generate_guard_rechaza_flight_report_no_commiteado(tmp_path: pathlib.Path) -> None:
    """Un FlightReport con transaction_status != committed (commit best-effort
    fallido) no alcanza: fail-closed, re-correr LOOT es barato."""
    journal = _journal_con_loot_completado()
    journal.get_last_operation.return_value = MagicMock(
        status=OperationStatus.COMPLETED,
        metadata={"kind": "flight_report", "transaction_status": "pending"},
    )
    service, colab = _servicio(tmp_path, journal=journal)

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "LOOT")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()


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
    # Sin lock NO somos dueños del estado: teardown() acá borraría el clon/mod
    # del run ACTIVO que sí tiene el lock (review Codex #291).
    colab["profile_manager"].teardown.assert_not_awaited()


async def test_generate_toma_tambien_el_lock_de_load_order(tmp_path: pathlib.Path) -> None:
    """El ritual lee loadorder.txt durante horas: además del lock grass-cache
    debe tomar load-order para excluir un sort de LOOT concurrente (review
    Codex #291)."""
    service, colab = _servicio(tmp_path, journal=_journal_con_loot_completado())

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True, resultado["message"]
    recursos = {c["resource_id"] for c in colab["llamadas_lock"]}
    assert recursos == {"grass-cache", "load-order"}


async def test_generate_load_order_ocupado_no_muta_ni_hace_teardown(tmp_path: pathlib.Path) -> None:
    """Si LOOT tiene el lock load-order, el ritual aborta limpio: sin Fase B
    y sin teardown (no se creó estado propio)."""
    tx_lo = _lock_tx_fake()
    tx_lo.__aenter__ = AsyncMock(side_effect=LockAcquisitionError("load-order", "loot-sorting-service"))
    service, colab = _servicio(
        tmp_path,
        journal=_journal_con_loot_completado(),
        lock_txs={"load-order": tx_lo},
    )

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "lock")
    colab["profile_manager"].create_clone_profile.assert_not_awaited()
    colab["profile_manager"].teardown.assert_not_awaited()


async def test_generate_teardown_corre_dentro_del_lock(tmp_path: pathlib.Path) -> None:
    """La Fase D restaura el entorno ANTES de liberar el lock: con el lock ya
    suelto, otro run podría clonar el perfil y este teardown se lo borraría
    (review Codex #291)."""
    orden: list[str] = []
    tx = _lock_tx_fake()

    async def _liberar(*_args: Any) -> bool:
        orden.append("lock_release")
        return False

    tx.__aexit__ = AsyncMock(side_effect=_liberar)
    service, colab = _servicio(tmp_path, journal=_journal_con_loot_completado(), lock_tx=tx)

    def _teardown() -> list[Any]:
        orden.append("teardown")
        return []

    colab["profile_manager"].teardown.side_effect = _teardown

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True, resultado["message"]
    assert orden.index("teardown") < orden.index("lock_release")


async def test_generate_fallo_al_liberar_el_lock_invalida_el_exito(tmp_path: pathlib.Path) -> None:
    """Si __aexit__ del lock falla (lease perdido con el auto-renew), la
    exclusividad no estuvo garantizada: el run NO puede reportarse exitoso ni
    commitear el journal aunque el runner haya terminado bien (review Codex
    #291)."""
    tx = _lock_tx_fake()
    tx.__aexit__ = AsyncMock(side_effect=RuntimeError("lease del lock perdido durante el run"))
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal, lock_tx=tx)

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "lease")
    journal.commit_transaction.assert_not_awaited()
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    colab["profile_manager"].teardown.assert_awaited()


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


async def test_generate_flag_sin_progreso_llega_al_contrato_y_al_journal(tmp_path: pathlib.Path) -> None:
    # El corte de relanzamiento (ronda 9) tiene que ser visible: outcome propio en el
    # resultado del tool, sin éxito, con teardown y la transacción del journal
    # marcada como fallida — no un "run raro" que se pierde en el journal.
    fallo = _resultado_runner(
        success=False,
        message="El flag de precache quedó SIN progreso y el caché parcial tiene 3 archivo(s)",
        outcome="flag_sin_progreso",
        cgid_count=3,
    )
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal, runner_result=fallo)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is False
    assert resultado["outcome"] == "flag_sin_progreso"
    assert "SIN progreso" in resultado["message"]
    colab["profile_manager"].teardown.assert_awaited()
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)


async def test_generate_cancelacion_hace_teardown_y_propaga(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    colab["runner"].run.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.generate(_PAYLOAD)

    colab["profile_manager"].teardown.assert_awaited()
    # La TX del journal no queda PENDING: rollback best-effort antes de
    # propagar la cancelación (review Codex #291).
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)


async def test_generate_fallo_loguea_stage_8(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """SOP §5 regla 5: todo fallo de tool loguea el stage index del pipeline."""
    import logging

    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    colab["profile_manager"].build_config_mod.side_effect = GrassProfileError("el clon ya existe")

    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.grass_cache_service"):
        await service.generate(_PAYLOAD)

    assert any("Stage 8" in registro.message for registro in caplog.records)


async def test_generate_keyboardinterrupt_cierra_journal_y_propaga(tmp_path: pathlib.Path) -> None:
    """§1.3: un BaseException que no es Exception (KeyboardInterrupt/SystemExit)
    NO debe saltear el cierre del journal — antes quedaba PENDING hasta el sweep
    de 24 h. Debe hacer teardown, marcar rolled-back y propagar."""
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    colab["runner"].run.side_effect = KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await service.generate(_PAYLOAD)

    colab["profile_manager"].teardown.assert_awaited()
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    journal.commit_transaction.assert_not_awaited()


async def test_generate_cancelacion_en_cierre_final_del_journal_reintenta_rollback(
    tmp_path: pathlib.Path,
) -> None:
    """Auditoría hostil (V-1): el cierre final del journal corre DESPUÉS de
    soltar los locks, fuera del ``except`` de cancelación de arriba. Una
    cancelación exacta durante ese commit no debe dejar la TX sin intentar
    cerrarla: se reintenta un rollback best-effort antes de propagar — pero
    solo si la TX sigue PENDING de verdad (ver test de abajo)."""
    journal = _journal_con_loot_completado()
    journal.commit_transaction.side_effect = asyncio.CancelledError
    journal.get_transaction.return_value = MagicMock(status=TransactionStatus.PENDING)
    service, colab = _servicio(tmp_path, journal=journal)  # runner exitoso por default

    with pytest.raises(asyncio.CancelledError):
        await service.generate(_PAYLOAD)

    journal.commit_transaction.assert_awaited_once_with(77)
    journal.get_transaction.assert_awaited_once_with(77)
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    colab["profile_manager"].teardown.assert_awaited()


async def test_generate_cancelacion_post_commit_no_pisa_una_tx_ya_committed(
    tmp_path: pathlib.Path,
) -> None:
    """Review Codex (PR #317, P2): la cancelación del ``commit_transaction()``
    final puede llegar DESPUÉS de que el UPDATE ya corrió en la BD —
    ``mark_transaction_rolled_back`` no valida el estado, así que un reintento
    ciego sobre-escribiría un ritual exitoso a rolled_back. Si al reconsultar
    la TX ya consta COMMITTED, el reintento NO debe tocarla."""
    journal = _journal_con_loot_completado()
    journal.commit_transaction.side_effect = asyncio.CancelledError
    journal.get_transaction.return_value = MagicMock(status=TransactionStatus.COMMITTED)
    service, colab = _servicio(tmp_path, journal=journal)

    with pytest.raises(asyncio.CancelledError):
        await service.generate(_PAYLOAD)

    journal.get_transaction.assert_awaited_once_with(77)
    journal.mark_transaction_rolled_back.assert_not_awaited()
    colab["profile_manager"].teardown.assert_awaited()


async def test_journal_close_fallo_de_bd_se_loguea(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """Auditoría hostil (A-1): el cierre best-effort del journal no debe
    tragar un fallo de BD en silencio — sin log, un commit/rollback roto
    desincroniza el journal sin dejar rastro."""
    import logging

    journal = _journal_con_loot_completado()
    journal.commit_transaction.side_effect = RuntimeError("constraint violation")
    service, _ = _servicio(tmp_path, journal=journal)

    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.grass_cache_service"):
        await service._journal_close(77, exito=True)

    assert any("journal" in registro.message.lower() for registro in caplog.records)


async def test_generate_expone_teardown_failures(tmp_path: pathlib.Path) -> None:
    """§1.6: si el teardown no pudo borrar el clon/mod, el resultado lo EXPONE
    (no se traga): el operador debe limpiar a mano o el próximo run fallará."""
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    trabado = tmp_path / "mo2" / "profiles" / "SkyClaw-GrassCache"
    colab["profile_manager"].teardown.return_value = [trabado]

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True, resultado["message"]
    assert resultado["teardown_failures"] == [str(trabado)]


async def test_generate_sin_fallos_de_teardown_no_agrega_la_clave(tmp_path: pathlib.Path) -> None:
    """En el happy path (teardown limpio) el resultado NO trae teardown_failures."""
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado())

    resultado = await service.generate(_PAYLOAD)

    assert "teardown_failures" not in resultado


# =============================================================================
# U-09 — el journal debe reflejar "producto OK, cleanup pendiente"
# =============================================================================


def _entradas_de_teardown(journal: AsyncMock) -> list[dict[str, Any]]:
    """Llamadas a ``begin_operation`` que marcan un teardown incompleto (U-09)."""
    return [
        llamada.kwargs
        for llamada in journal.begin_operation.await_args_list
        if is_teardown_incomplete(llamada.kwargs.get("metadata"))
    ]


def _entradas_de_veredicto(journal: AsyncMock) -> list[dict[str, Any]]:
    """Llamadas a ``begin_operation`` que registran el veredicto del precache (P1)."""
    return [
        llamada.kwargs
        for llamada in journal.begin_operation.await_args_list
        if llamada.kwargs.get("metadata", {}).get("kind") == GRASS_VERDICT_KIND
    ]


async def test_teardown_fallido_commitea_pero_deja_constancia_en_el_journal(tmp_path: pathlib.Path) -> None:
    """U-09: el cache SÍ se generó, así que la TX se commitea — pero el journal
    tiene que registrar que el cleanup quedó a medias.

    Antes, ``teardown_failures`` solo viajaba en el dict de retorno y la TX se
    commiteaba como éxito limpio: el audit trail afirmaba que el FS quedó
    consistente cuando el clon/mod seguían en disco. NO se puede marcar la TX
    ROLLED_BACK (mentiría en la otra dirección: el producto existe y se preserva),
    así que el estado mixto se expresa como una operación FALLIDA dentro de la TX
    committeada.
    """
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    trabado = tmp_path / "mo2" / "profiles" / "SkyClaw-GrassCache"
    colab["profile_manager"].teardown.return_value = [trabado]

    resultado = await service.generate(_PAYLOAD)

    # El producto está: ni el resultado ni la TX se degradan.
    assert resultado["success"] is True, resultado["message"]
    journal.commit_transaction.assert_awaited_once_with(77)
    journal.mark_transaction_rolled_back.assert_not_awaited()

    # ...pero queda constancia del cleanup pendiente, en la MISMA TX.
    entradas = _entradas_de_teardown(journal)
    assert len(entradas) == 1, "falta la entrada de teardown incompleto en el journal"
    assert entradas[0]["transaction_id"] == 77
    assert entradas[0]["metadata"]["paths"] == [str(trabado)]
    # P1: la TX ahora registra DOS operaciones fallidas distintas — el teardown incompleto (U-09) y
    # el veredicto no certificado —, así que el conteo global ya no es 1. Cada una se identifica por
    # su `kind` (el filtro es el mismo mecanismo que usa el predicado de lectura).
    assert len(_entradas_de_veredicto(journal)) == 1, "falta la operación del veredicto"
    assert journal.fail_operation.await_count == 2


async def test_teardown_limpio_no_ensucia_el_journal(tmp_path: pathlib.Path) -> None:
    """Sin fallos de teardown no se registra ninguna entrada del marcador U-09."""
    journal = _journal_con_loot_completado()
    service, _ = _servicio(tmp_path, journal=journal)

    await service.generate(_PAYLOAD)

    assert _entradas_de_teardown(journal) == []
    journal.commit_transaction.assert_awaited_once_with(77)


async def test_teardown_incompleto_se_registra_aunque_el_ritual_falle(tmp_path: pathlib.Path) -> None:
    """U-09 (review Qodo): la constancia NO se condiciona a que el run haya salido bien.

    Se sugirió registrarla solo cuando ``exito`` es True, con el argumento de que
    en una TX revertida el marcador sería semánticamente inexacto. El marcador
    afirma "estos paths quedaron sin limpiar", no "el producto está OK": los
    residuos son igual de reales cuando el run falló, y ahí el dato le sirve MÁS
    al operador. Además ``result["teardown_failures"]`` tampoco se condiciona a
    ``exito`` (comportamiento previo a este PR), así que suprimir el journalizado
    dejaría al journal afirmando menos que el dict de retorno sobre el mismo run
    — exactamente la divergencia que U-09 viene a cerrar.
    """
    journal = _journal_con_loot_completado()
    service, colab = _servicio(tmp_path, journal=journal)
    colab["profile_manager"].build_config_mod.side_effect = GrassProfileError("el clon ya existe")
    trabado = tmp_path / "mo2" / "profiles" / "SkyClaw-GrassCache"
    colab["profile_manager"].teardown.return_value = [trabado]

    resultado = await service.generate(_PAYLOAD)

    # El ritual fracasó y la TX se revierte...
    assert resultado["success"] is False
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    # ...pero los residuos en disco quedan registrados igual, en la misma TX.
    entradas = _entradas_de_teardown(journal)
    assert len(entradas) == 1
    assert entradas[0]["transaction_id"] == 77, "el residuo debe colgar de la TX del ritual"
    assert entradas[0]["metadata"]["paths"] == [str(trabado)]
    # La operación queda FALLIDA (no pendiente): es lo que la vuelve legible como
    # "cleanup que no se completó" y no como un paso más del ritual.
    journal.fail_operation.assert_awaited_once_with(5, error=str(trabado))
    # Y el journal no dice menos que el dict de retorno.
    assert resultado["teardown_failures"] == [str(trabado)]


async def test_teardown_incompleto_sobrevive_al_rollback_en_el_journal_real(tmp_path: pathlib.Path) -> None:
    """U-09 (review Qodo #377): la constancia PERSISTE en la DB real tras revertir la TX.

    La objeción fue de "falsa confianza por mock": que en producción
    ``mark_transaction_rolled_back`` descartaría físicamente las filas insertadas
    bajo esa transacción, dejando al operador sin rastro. **No es así**, y este
    test lo prueba contra SQLite en vez de argumentarlo: las "transacciones" del
    journal son un concepto de DOMINIO (una fila en ``transactions`` con columna
    ``status``), no transacciones de la base — ``mark_transaction_rolled_back``
    hace ``UPDATE ... SET status='rolled_back'`` + ``commit()``, y no existe
    ningún ``db.rollback()`` en ``journal.py``. Cada ``begin_operation`` commitea
    su propio ``INSERT``.

    El propio diseño depende de esto: el guard Stage 5→8 lee operaciones de LOOT
    de TXs ya cerradas, y no funcionaría si el cierre las borrara.
    """
    from sky_claw.app.db.journal import OperationJournal, OperationType

    journal = OperationJournal(tmp_path / "grass_journal.db")
    await journal.open()
    try:
        # Sembrar el Stage 5 (LOOT) que el guard exige, con FlightReport commiteado.
        loot_tx = await journal.begin_transaction("LOOT sort", agent_id=GrassCacheService.LOOT_AGENT_ID)
        loot_op = await journal.begin_operation(
            agent_id=GrassCacheService.LOOT_AGENT_ID,
            operation_type=OperationType.FILE_MODIFY,
            target_path="plugins.txt",
            transaction_id=loot_tx,
            metadata={"kind": "flight_report", "transaction_status": TransactionStatus.COMMITTED.value},
        )
        await journal.complete_operation(loot_op)
        await journal.commit_transaction(loot_tx)

        service, colab = _servicio(tmp_path, journal=journal)
        colab["profile_manager"].build_config_mod.side_effect = GrassProfileError("el clon ya existe")
        trabado = tmp_path / "mo2" / "profiles" / "SkyClaw-GrassCache"
        colab["profile_manager"].teardown.return_value = [trabado]

        resultado = await service.generate(_PAYLOAD)
        assert resultado["success"] is False

        # La TX del ritual quedó revertida. Se busca por descripción y no por
        # "la más reciente": la TX sembrada de LOOT comparte el mismo segundo
        # (``datetime('now')``) y el desempate no sería determinista.
        transacciones = await journal.list_recent_transactions(limit=10)
        (ritual,) = [tx for tx in transacciones if "Grass precache" in tx.description]
        assert ritual.status == TransactionStatus.ROLLED_BACK
        # ...y aun así la constancia del residuo SIGUE consultable en la DB.
        operaciones = await journal.get_operations_by_transaction(ritual.transaction_id)
        residuos = [op for op in operaciones if is_teardown_incomplete(op.metadata)]
        assert len(residuos) == 1, "el rollback de la TX no debe borrar la constancia del residuo"
        assert residuos[0].metadata["paths"] == [str(trabado)]
        assert residuos[0].status == OperationStatus.FAILED
    finally:
        await journal.close()


async def test_journalizar_el_teardown_incompleto_es_best_effort(tmp_path: pathlib.Path) -> None:
    """El journal NUNCA enmascara el resultado del ritual (espejo de ``_journal_close``).

    Si registrar la constancia falla, el cache generado sigue siendo un éxito y la
    TX se commitea igual: perder la anotación es peor que perder el run, pero
    romper el run por no poder anotar sería peor todavía.
    """
    journal = _journal_con_loot_completado()
    journal.begin_operation.side_effect = OSError("journal caído")
    service, colab = _servicio(tmp_path, journal=journal)
    trabado = tmp_path / "mo2" / "profiles" / "SkyClaw-GrassCache"
    colab["profile_manager"].teardown.return_value = [trabado]

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True, resultado["message"]
    assert resultado["teardown_failures"] == [str(trabado)]
    journal.commit_transaction.assert_awaited_once_with(77)


def test_contrato_flight_report_lectura_escritura() -> None:
    """§2.2: el helper de lectura (is_flight_report_committed) concuerda con lo
    que escribe el modelo FlightReport de LOOT — mismo kind y transaction_status."""
    from sky_claw.app.db.journal import TransactionStatus
    from sky_claw.app.db.journal_contracts import FLIGHT_REPORT_KIND, is_flight_report_committed
    from sky_claw.app.orchestrator.preview.manifest import FlightReport

    commiteado = FlightReport(transaction_status=TransactionStatus.COMMITTED.value).model_dump(mode="json")
    assert commiteado["kind"] == FLIGHT_REPORT_KIND
    assert is_flight_report_committed(commiteado) is True

    revertido = FlightReport(transaction_status=TransactionStatus.ROLLED_BACK.value).model_dump(mode="json")
    assert is_flight_report_committed(revertido) is False
    # Un ActionManifest (sin kind=flight_report) tampoco alcanza.
    assert is_flight_report_committed({"ritual_id": "loot-sort-1", "transaction_status": "committed"}) is False
    assert is_flight_report_committed(None) is False


def test_contrato_teardown_incompleto_lectura_escritura() -> None:
    """U-09: el marcador del teardown incompleto vive en el mismo módulo de
    contratos que el del FlightReport, por el mismo motivo — que el lado que
    escribe y el que lee no se desincronicen con strings sueltos."""
    from sky_claw.app.db.journal_contracts import TEARDOWN_INCOMPLETE_KIND

    assert is_teardown_incomplete({"kind": TEARDOWN_INCOMPLETE_KIND, "paths": ["/x/clon"]}) is True
    # Otro kind (p. ej. el FlightReport de LOOT en la misma TX) no lo confunde.
    assert is_teardown_incomplete({"kind": "flight_report", "transaction_status": "committed"}) is False
    assert is_teardown_incomplete({}) is False
    assert is_teardown_incomplete(None) is False
    assert is_teardown_incomplete("no-es-un-mapping") is False


async def test_generate_resuelve_deps_por_provider_lazy(tmp_path: pathlib.Path) -> None:
    """Las deps de Fases B/C pueden venir de un provider lazy (no solo concretas):
    se resuelven al ejecutar el ritual, tras la hidratación de entorno (Codex #301)."""
    game = tmp_path / "game"
    game.mkdir()
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    (tmp_path / "mo2" / "overwrite").mkdir(parents=True)

    profile_manager = AsyncMock()
    profile_manager.clone_profile = "SkyClaw-GrassCache"
    # El clon (LANZAMIENTO) y el perfil de ORIGEN (identidad del progreso)
    # son nombres distintos: el mock declara los dos.
    profile_manager.source_profile = "Default"
    profile_manager.teardown.return_value = []
    runner = AsyncMock()
    runner.run.return_value = _resultado_runner()
    llamadas = {"provider": 0}

    def _provider() -> GrassRuntimeDeps:
        llamadas["provider"] += 1
        return GrassRuntimeDeps(
            profile_manager=profile_manager,
            mo2=MagicMock(),
            game_path=game,
            overwrite_grass_dir=tmp_path / "mo2" / "overwrite" / "Grass",
        )

    service = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=_journal_con_loot_completado(),
        event_bus=AsyncMock(),
        runner_factory=MagicMock(return_value=runner),
        lock_factory=lambda **_kw: _lock_tx_fake(),
        runtime_deps_provider=_provider,
    )

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True, resultado["message"]
    profile_manager.create_clone_profile.assert_awaited_once()
    assert llamadas["provider"] == 1


async def test_generate_provider_devuelve_none_es_error_de_contrato(tmp_path: pathlib.Path) -> None:
    """Si el entorno aún no está configurado, el provider devuelve None y el
    servicio responde con error de contrato accionable (sin deps concretas)."""
    service = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=_journal_con_loot_completado(),
        event_bus=AsyncMock(),
        lock_factory=lambda **_kw: _lock_tx_fake(),
        runtime_deps_provider=lambda: None,
    )

    resultado = await service.generate(_PAYLOAD)

    _assert_error_de_contrato(resultado, "no configurada")


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


# =============================================================================
# U-03 — Reconciliación de arranque: barrer PrecacheGrass.txt huérfano
# =============================================================================
#
# Muerte dura (SIGKILL/OOM/corte de luz) durante el precache: el `finally` del
# runner no corre, así que `PrecacheGrass.txt` queda junto a SkyrimSE.exe. La
# próxima vez que el usuario abra el juego, NGIO ve el flag y re-entra en modo
# precache (crash-loop 800x400). El hook de arranque lo barre — pero NUNCA si hay
# un ritual de grass EN CURSO (lock `grass-cache` vivo, posiblemente en otra
# instancia): ese flag es legítimo.


def _lock_info_vivo(offset_s: float) -> Any:
    """LockInfo de `grass-cache` cuyo vencimiento está a *offset_s* de ahora."""
    import time

    from sky_claw.app.db.locks import LockInfo
    from sky_claw.local.tools.grass_cache_service import GRASS_CACHE_RESOURCE_ID

    now = time.time()
    return LockInfo(
        resource_id=GRASS_CACHE_RESOURCE_ID,
        agent_id="grass-cache-service",
        acquired_at=now,
        expires_at=now + offset_s,
    )


def _lock_manager(info: Any = None, *, acquire_falla: bool = False) -> AsyncMock:
    """LockManager falso: ``get_lock_info``→*info*; ``acquire_lock`` opcionalmente falla.

    ``acquire_falla=True`` modela la intercalación del TOCTOU: otra instancia gana
    el lock entre el chequeo inicial y la adquisición, así que ``acquire_lock``
    lanza ``LockAcquisitionError``.
    """
    mgr = AsyncMock()
    mgr.get_lock_info = AsyncMock(return_value=info)
    if acquire_falla:
        mgr.acquire_lock = AsyncMock(side_effect=LockAcquisitionError("grass-cache", "otra-instancia", "ocupado"))
    else:
        mgr.acquire_lock = AsyncMock(return_value=MagicMock())
    mgr.release_lock = AsyncMock(return_value=True)
    return mgr


async def test_reconcilia_flag_huerfano_sin_lock_lo_borra(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.tools.grass_cache_service import reconcile_orphan_precache_flag

    flag = tmp_path / "PrecacheGrass.txt"
    flag.write_bytes(b"")
    mgr = _lock_manager(None)  # sin lock → no hay ritual activo

    removed = await reconcile_orphan_precache_flag(tmp_path, mgr)

    assert removed is True
    assert not flag.exists(), "el flag huérfano debe borrarse cuando no hay ritual activo"
    # El borrado se hizo bajo el lock, adquirido y liberado (atómico, no un simple check).
    mgr.acquire_lock.assert_awaited_once()
    mgr.release_lock.assert_awaited_once()


async def test_no_toca_flag_con_ritual_activo(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.tools.grass_cache_service import reconcile_orphan_precache_flag

    flag = tmp_path / "PrecacheGrass.txt"
    flag.write_bytes(b"")
    mgr = _lock_manager(_lock_info_vivo(600.0))  # lock no expirado → ritual en curso

    removed = await reconcile_orphan_precache_flag(tmp_path, mgr)

    assert removed is False
    assert flag.exists(), "un flag de un ritual EN CURSO jamás debe borrarse"
    # Fast-path: ni siquiera intenta adquirir (evita el backoff ante un lock tomado).
    mgr.acquire_lock.assert_not_awaited()


async def test_race_ritual_gana_el_lock_no_borra(tmp_path: pathlib.Path) -> None:
    """TOCTOU: el chequeo inicial no ve lock, pero otra instancia lo adquiere antes
    del borrado. ``acquire_lock`` falla → NO se borra el flag legítimo del ritual."""
    from sky_claw.local.tools.grass_cache_service import reconcile_orphan_precache_flag

    flag = tmp_path / "PrecacheGrass.txt"
    flag.write_bytes(b"")
    # get_lock_info=None (al chequear no había lock) pero acquire_lock falla:
    # el ritual ganó la carrera entre el chequeo y la adquisición.
    mgr = _lock_manager(None, acquire_falla=True)

    removed = await reconcile_orphan_precache_flag(tmp_path, mgr)

    assert removed is False
    assert flag.exists(), "si el ritual ganó el lock, su flag no se borra pese al chequeo previo"
    mgr.acquire_lock.assert_awaited_once()
    mgr.release_lock.assert_not_awaited()  # nunca adquirimos → nada que liberar


async def test_lock_expirado_se_trata_como_huerfano(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.tools.grass_cache_service import reconcile_orphan_precache_flag

    flag = tmp_path / "PrecacheGrass.txt"
    flag.write_bytes(b"")
    mgr = _lock_manager(_lock_info_vivo(-1.0))  # ya vencido → el dueño murió sin liberar

    removed = await reconcile_orphan_precache_flag(tmp_path, mgr)

    assert removed is True
    assert not flag.exists(), "un lock vencido no protege el flag: es huérfano"


async def test_sin_flag_es_noop(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.tools.grass_cache_service import reconcile_orphan_precache_flag

    mgr = _lock_manager(None)

    removed = await reconcile_orphan_precache_flag(tmp_path, mgr)

    assert removed is False
    assert not (tmp_path / "PrecacheGrass.txt").exists()
    mgr.acquire_lock.assert_not_awaited()  # sin flag → ni consulta el lock


async def test_generate_captura_runtime_error_de_resolucion_de_raices() -> None:
    """GrassCacheService.generate convierte RuntimeError de resolución de raíces
    en resultado estructurado de error (GrassCacheServiceError -> success: False, message: ...)
    sin propagar excepción cruda.
    """

    def provider_con_error():
        raise RuntimeError("mod_directory declarado pero no disponible en disco")

    service = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        runtime_deps_provider=provider_con_error,
    )
    result = await service.generate({"worldspaces": ["Tamriel"], "force_stage_guard": True})
    assert result["success"] is False
    assert "Error resolviendo dependencias de MO2 para grass cache" in result["message"]
    assert "mod_directory declarado pero no disponible en disco" in result["message"]


# ---------------------------------------------------------------------------
# P0 SAFE CACHE (F-01) — el servicio no clona un perfil para un run inseguro
# ---------------------------------------------------------------------------


async def test_payload_rebuild_sin_aprobacion_cumple_contrato(tmp_path: pathlib.Path) -> None:
    # La aprobación se exige en el payload: no se le pide al operador que
    # apruebe algo que el runner va a rechazar de todos modos.
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())

    resultado = await service.generate({**_PAYLOAD, "mode": "rebuild"})

    _assert_error_de_contrato(resultado, "rebuild")
    colaboradores["runner"].run.assert_not_awaited()
    colaboradores["profile_manager"].create_clone_profile.assert_not_awaited()


async def test_modo_invalido_cumple_contrato(tmp_path: pathlib.Path) -> None:
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado())

    resultado = await service.generate({**_PAYLOAD, "mode": "destruir-todo"})

    _assert_error_de_contrato(resultado, "payload")


async def test_generate_no_clona_perfil_con_cache_preexistente_sin_decision(tmp_path: pathlib.Path) -> None:
    # El punto del guard: el rechazo ocurre ANTES de tocar el árbol MO2 (clonar
    # un perfil y crear el mod de config para un run condenado era trabajo inútil).
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())
    grass = service._overwrite_grass_dir
    assert grass is not None
    grass.mkdir(parents=True)
    (grass / "previo.cgid").write_bytes(b"prec")

    resultado = await service.generate(dict(_PAYLOAD))

    _assert_error_de_contrato(resultado, "caché")
    colaboradores["profile_manager"].create_clone_profile.assert_not_awaited()
    colaboradores["runner"].run.assert_not_awaited()
    assert (grass / "previo.cgid").read_bytes() == b"prec", "el caché previo no se toca"


async def test_generate_rechaza_no_truncar_flag_con_progreso(tmp_path: pathlib.Path) -> None:
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())
    flag = tmp_path / "game" / "PrecacheGrass.txt"
    progreso = "Tamriel\n-2,4\n"
    flag.write_bytes(progreso.encode("utf-8"))

    resultado = await service.generate(dict(_PAYLOAD))

    _assert_error_de_contrato(resultado, "progreso")
    colaboradores["runner"].run.assert_not_awaited()
    assert flag.read_text(encoding="utf-8") == progreso


async def test_el_modo_y_la_raiz_de_estado_viajan_al_runner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())
    estado = tmp_path / "estado"
    monkeypatch.setattr(GrassCacheService, "_raiz_de_estado", staticmethod(lambda: estado))
    (tmp_path / "game" / "PrecacheGrass.txt").write_bytes(b"Tamriel\n")

    resultado = await service.generate({**_PAYLOAD, "mode": "resume"})

    assert resultado["success"] is True
    config = colaboradores["runner_factory"].call_args.args[0]
    assert config.mode == "resume"
    assert config.backup_root == estado


async def test_rebuild_aprobado_respalda_antes_de_clonar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())
    estado = tmp_path / "estado"
    monkeypatch.setattr(GrassCacheService, "_raiz_de_estado", staticmethod(lambda: estado))
    grass = service._overwrite_grass_dir
    assert grass is not None
    grass.mkdir(parents=True)
    (grass / "previo.cgid").write_bytes(b"prec")

    resultado = await service.generate(
        {**_PAYLOAD, "mode": "rebuild", "rebuild_approval": "REBUILD", "ack_usvfs_no_verificado": True}
    )

    assert resultado["success"] is True
    respaldos = list((estado / "respaldo").iterdir())
    assert respaldos, "el respaldo queda verificado en la raíz de estado durable"
    assert (respaldos[0] / "previo.cgid").read_bytes() == b"prec"
    colaboradores["profile_manager"].create_clone_profile.assert_awaited()


async def test_fallo_del_guard_no_deja_transaccion_pendiente(tmp_path: pathlib.Path) -> None:
    # El rechazo de P0 no muta nada: el journal no registra una operación.
    journal = _journal_con_loot_completado()
    service, _ = _servicio(tmp_path, journal=journal)
    grass = service._overwrite_grass_dir
    assert grass is not None
    grass.mkdir(parents=True)
    (grass / "previo.cgid").write_bytes(b"prec")

    await service.generate(dict(_PAYLOAD))

    journal.begin_transaction.assert_not_awaited()


async def test_generate_rebuild_sin_ack_de_usvfs_queda_en_hold(tmp_path: pathlib.Path) -> None:
    # Sin cobertura USVFS demostrada, el servicio tampoco clona ni respalda.
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())
    grass = service._overwrite_grass_dir
    assert grass is not None
    grass.mkdir(parents=True)
    (grass / "previo.cgid").write_bytes(b"prec")

    resultado = await service.generate(
        {**_PAYLOAD, "mode": "rebuild", "rebuild_approval": "REBUILD", "ack_usvfs_no_verificado": False}
    )

    _assert_error_de_contrato(resultado, "USVFS")
    colaboradores["profile_manager"].create_clone_profile.assert_not_awaited()
    colaboradores["runner"].run.assert_not_awaited()


async def test_flag_residual_llega_al_resultado_y_al_teardown(tmp_path: pathlib.Path) -> None:
    # Un flag que no se pudo borrar es limpieza incompleta: viaja en el
    # resultado (y por la misma vía que el teardown), nunca en silencio.
    runner_result = _resultado_runner(flag_residual=True, limpieza_errores=("archivo en uso",))
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado(), runner_result=runner_result)

    resultado = await service.generate(dict(_PAYLOAD))

    # B1: un flag que quedó en disco deja al juego del usuario arrancando en
    # modo precache; eso NO es un éxito, aunque el ritual haya terminado.
    assert resultado["success"] is False
    assert resultado["flag_residual"] is True
    assert any("PrecacheGrass.txt residual" in fallo for fallo in resultado["teardown_failures"])
    assert any("archivo en uso" in fallo for fallo in resultado["teardown_failures"])


async def test_un_flag_alternativo_residual_invalida_el_exito(tmp_path: pathlib.Path) -> None:
    # El alternativo (`PrecacheGrass.txt.txt`) no lo escribe Sky-Claw y no se
    # borra, pero mientras exista NGIO puede abrirlo como flag de progreso: el
    # éxito del ritual no alcanza y el teardown lo declara.
    runner_result = _resultado_runner(
        flag_alternativo_residual=True,
        limpieza_errores=("Queda el flag ALTERNATIVO PrecacheGrass.txt.txt en disco",),
    )
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado(), runner_result=runner_result)

    resultado = await service.generate(dict(_PAYLOAD))

    assert resultado["success"] is False
    assert resultado["flag_alternativo_residual"] is True
    assert any("alternativo residual" in fallo for fallo in resultado["teardown_failures"])
    assert any("ALTERNATIVO" in fallo for fallo in resultado["teardown_failures"])


async def test_generate_new_sin_ack_de_usvfs_queda_en_hold(tmp_path: pathlib.Path) -> None:
    # B5: NEW escribe el flag vacío; con la cobertura USVFS sin demostrar el
    # servicio tampoco clona el perfil ni lanza el runner.
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())

    resultado = await service.generate({**_PAYLOAD, "ack_usvfs_no_verificado": False})

    _assert_error_de_contrato(resultado, "USVFS")
    colaboradores["profile_manager"].create_clone_profile.assert_not_awaited()
    colaboradores["runner"].run.assert_not_awaited()


async def test_progreso_perdido_no_declara_exito(tmp_path: pathlib.Path) -> None:
    # B4/B1: perder la evidencia de reanudación es una limpieza incompleta.
    runner_result = _resultado_runner(
        progreso_perdido=True, limpieza_errores=("No se pudo archivar el progreso: disco lleno",)
    )
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado(), runner_result=runner_result)

    resultado = await service.generate(dict(_PAYLOAD))

    assert resultado["success"] is False
    assert resultado["progreso_perdido"] is True
    assert any("progreso" in fallo.lower() for fallo in resultado["teardown_failures"])


# ---------------------------------------------------------------------------
# Hallazgo de revisión: cancelación mientras el respaldo se prepara (locks vivos)
# ---------------------------------------------------------------------------


async def test_cancelacion_durante_el_preflight_no_abandona_el_respaldo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `preparar_arranque` copia el respaldo en un HILO. Si la cancelación llega
    # mientras copia, el `to_thread` levanta CancelledError y el `async with`
    # suelta los DOS locks con el hilo todavía escribiendo: un reintento entraría
    # a los mismos caminos de respaldo/progreso en paralelo. La cancelación no
    # puede propagarse hasta que el hilo termine.
    import threading

    import sky_claw.local.tools.grass_cache_service as svc_mod

    en_copia = threading.Event()
    liberar = threading.Event()
    termino = threading.Event()

    def _preflight_lento(*args: Any, **kwargs: Any) -> Any:
        en_copia.set()
        liberar.wait(timeout=10)
        termino.set()
        return MagicMock()

    monkeypatch.setattr(svc_mod, "preparar_arranque", _preflight_lento)
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())

    tarea = asyncio.ensure_future(service.generate(_PAYLOAD))
    assert await asyncio.to_thread(en_copia.wait, 5.0), "el preflight no llegó a empezar"
    tarea.cancel()
    await asyncio.sleep(0)
    liberar.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10)

    assert termino.is_set(), "la cancelación se propagó con el respaldo todavía en vuelo"
    colaboradores["profile_manager"].create_clone_profile.assert_not_awaited()
    assert colaboradores["llamadas_lock"], "los locks se pidieron antes del preflight"


async def test_cancelacion_al_anotar_el_teardown_incompleto_no_deja_la_tx_pendiente(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # La anotación U-09 ("limpieza incompleta") corre justo ANTES del cierre y
    # fuera del handler de cancelación: una cancelación exacta ahí escapaba sin
    # ningún intento de cerrar la TX, que quedaba PENDING hasta el sweep de 24 h.
    journal = _journal_con_loot_completado()
    journal.get_transaction.return_value = MagicMock(status=TransactionStatus.PENDING)
    journal.commit_transaction.side_effect = None
    service, colab = _servicio(
        tmp_path,
        journal=journal,
        runner_result=_resultado_runner(success=False, flag_residual=True, limpieza_errores=("unlink bloqueado",)),
    )

    async def _anotacion_cancelada(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_journalizar_teardown_incompleto", _anotacion_cancelada)

    with pytest.raises(asyncio.CancelledError):
        await service.generate(_PAYLOAD)

    journal.get_transaction.assert_awaited_once_with(77)
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    colab["profile_manager"].teardown.assert_awaited()


# ---------------------------------------------------------------------------
# F1: identidad del progreso = perfil de ORIGEN (no el nombre del clon)
# ---------------------------------------------------------------------------


def _arbol_mo2(tmp_path: pathlib.Path, perfiles: tuple[str, ...]) -> pathlib.Path:
    """Instancia MO2 sintética con los perfiles pedidos y ``overwrite/``."""
    root = tmp_path / "mo2"
    for perfil in perfiles:
        directorio = root / "profiles" / perfil
        directorio.mkdir(parents=True, exist_ok=True)
        (directorio / "modlist.txt").write_bytes(b"\xef\xbb\xbf+ModA\r\n")
        (directorio / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n")
        (directorio / "Skyrim.ini").write_bytes(b"[General]\r\n")
        (directorio / "settings.txt").write_bytes(b"[General]\r\n")
    (root / "mods").mkdir(exist_ok=True)
    (root / "overwrite").mkdir(exist_ok=True)
    return root


def test_build_runner_config_separa_lanzamiento_de_identidad_de_progreso(tmp_path: pathlib.Path) -> None:
    # El clon dedicado es CONSTANTE entre perfiles: si la identidad del progreso
    # sale del clon, dos perfiles de origen comparten evidencia. El camino
    # productivo (``_build_runner_config`` con un manager REAL) tiene que
    # declarar los dos nombres por separado.
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.local.mo2.grass_profile import GrassProfileManager
    from sky_claw.local.tools.grass_cache_service import GenerateGrassCacheParams

    mo2 = _arbol_mo2(tmp_path, ("PerfilA",))
    manager = GrassProfileManager(mo2, PathValidator(roots=[mo2]), source_profile="PerfilA")
    service = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        game_path=mo2 / "game",
        overwrite_grass_dir=mo2 / "overwrite" / "Grass",
    )
    (mo2 / "game").mkdir()
    (mo2 / "game" / "SkyrimSE.exe").write_bytes(b"MZ")

    config = service._build_runner_config(GenerateGrassCacheParams(worldspaces=["Tamriel"]), manager)

    assert config.profile == "SkyClaw-GrassCache", "el LANZAMIENTO sigue usando el clon dedicado"
    assert config.progress_profile == "PerfilA", "la identidad del progreso es el perfil de ORIGEN"
    assert config.perfil_de_progreso == "PerfilA"


async def test_generate_transmite_el_perfil_de_origen_al_runner(tmp_path: pathlib.Path) -> None:
    # El servicio REAL (no una GrassCacheConfig armada a mano) es quien cablea la
    # identidad: se intercepta la construcción del runner y se mira la config.
    service, colaboradores = _servicio(tmp_path, journal=_journal_con_loot_completado())
    colaboradores["profile_manager"].source_profile = "PerfilA"

    await service.generate(_PAYLOAD)

    config = colaboradores["runner_factory"].call_args.args[0]
    assert config.profile == "SkyClaw-GrassCache"
    assert config.progress_profile == "PerfilA"
    assert config.perfil_de_progreso == "PerfilA"


async def test_dos_perfiles_de_origen_no_se_prestan_el_progreso(tmp_path: pathlib.Path) -> None:
    # Integración: el mismo servicio, dos perfiles de origen sobre la MISMA
    # instalación (mismo overwrite/Grass y mismo clon). El progreso que archiva
    # A no puede reanudar el run de B.
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.local.mo2.grass_profile import GrassProfileManager
    from sky_claw.local.tools.grass_cache_safety import (
        GrassCacheSafetyError,
        archivar_progreso,
        inventariar_cache,
        preparar_arranque,
        ruta_del_flag,
    )

    mo2 = _arbol_mo2(tmp_path, ("PerfilA", "PerfilB"))
    grass = mo2 / "overwrite" / "Grass"
    grass.mkdir(parents=True)
    (grass / "Tamriel.cgid").write_bytes(b"celda-tamriel")
    juego = mo2 / "game"
    juego.mkdir()
    (juego / "SkyrimSE.exe").write_bytes(b"MZ")
    estado = tmp_path / "estado"

    manager_a = GrassProfileManager(mo2, PathValidator(roots=[mo2]), source_profile="PerfilA")
    manager_b = GrassProfileManager(mo2, PathValidator(roots=[mo2]), source_profile="PerfilB")
    servicio = GrassCacheService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        game_path=juego,
        overwrite_grass_dir=grass,
    )
    config_a = servicio._build_runner_config(GenerateGrassCacheParams(worldspaces=["Tamriel"]), manager_a)
    config_b = servicio._build_runner_config(GenerateGrassCacheParams(worldspaces=["Tamriel"]), manager_b)
    assert config_a.perfil_de_progreso != config_b.perfil_de_progreso

    inventario = inventariar_cache(grass_dir=grass, flag_path=ruta_del_flag(juego))
    archivar_progreso(estado, inventario, b"Tamriel\n-2,4\n", grass_dir=grass, perfil=config_a.perfil_de_progreso)

    # A reanuda con SU propia evidencia.
    plan_a = preparar_arranque(
        modo="resume",
        game_path=juego,
        grass_dir=grass,
        raiz_estado=estado,
        perfil=config_a.perfil_de_progreso,
    )
    assert plan_a.accion == "restaurar"
    assert plan_a.contenido == b"Tamriel\n-2,4\n"

    # B no puede quedarse con el progreso de A.
    with pytest.raises(GrassCacheSafetyError) as excinfo:
        preparar_arranque(
            modo="resume",
            game_path=juego,
            grass_dir=grass,
            raiz_estado=estado,
            perfil=config_b.perfil_de_progreso,
        )
    assert excinfo.value.codigo == "progreso_de_otra_instalacion"


# ---------------------------------------------------------------------------
# P1 · propagación del veredicto (C5): servicio, journal y evento
# ---------------------------------------------------------------------------


async def test_p1_el_resultado_propaga_veredicto_alcance_y_evidencia(tmp_path: pathlib.Path) -> None:
    """C5: el veredicto llega al dict sin pérdida. `success` NO es certificación (D1)."""
    evidencia = {
        "worldspaces_solicitados": ["Tamriel"],
        "archivos_heredados": 12,
        "archivos_nuevos": 0,
        "terminalidad": "terminado",
        "completitud_de_celdas": "no_verificada",
    }
    runner_result = _resultado_runner(
        success=False,
        verdict="completed_unverified",
        certificado=False,
        verdict_razones=("archivos_heredados_sin_procedencia",),
        alcance_certificado="ninguno",
        evidencia=evidencia,
        limpieza_errores=("unlink falló",),
    )
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado(), runner_result=runner_result)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["verdict"] == "completed_unverified"
    assert resultado["certificado"] is False
    assert resultado["success"] is False
    assert resultado["verdict_razones"] == ["archivos_heredados_sin_procedencia"]
    assert resultado["alcance_certificado"] == "ninguno"
    assert resultado["evidencia"] == evidencia
    # `limpieza_errores` se perdería en la capa de servicio; P1 la propaga.
    assert resultado["limpieza_errores"] == ["unlink falló"]


async def test_p1_success_true_con_certificado_false_no_se_reinterpreta(tmp_path: pathlib.Path) -> None:
    """El servicio NO re-deriva: propaga lo que decidió la política (una sola fuente)."""
    runner_result = _resultado_runner(
        success=True,
        verdict="completed_unverified",
        certificado=False,
        verdict_razones=("fallos_de_celda_atribuibles",),
    )
    service, _ = _servicio(tmp_path, journal=_journal_con_loot_completado(), runner_result=runner_result)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True
    assert resultado["certificado"] is False
    assert resultado["verdict"] == "completed_unverified"


async def test_p1_journal_registra_el_veredicto_certificado(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        verdict="completed_verified",
        certificado=True,
        alcance_certificado="corrida_completa",
        evidencia={"worldspaces_solicitados": ["Tamriel"], "overwrite_grass_dir": "/x/Grass"},
    )
    service, _ = _servicio(tmp_path, journal=journal, runner_result=runner_result)

    await service.generate(_PAYLOAD)

    entradas = _entradas_de_veredicto(journal)
    assert len(entradas) == 1
    metadata = entradas[0]["metadata"]
    assert metadata["kind"] == GRASS_VERDICT_KIND
    assert metadata["verdict"] == "completed_verified"
    assert metadata["certificado"] is True
    assert metadata["worldspaces"] == ["Tamriel"]
    # La metadata del veredicto NO siembra evidencia de artefactos (oráculo de recovery).
    assert "files_touched" not in metadata
    journal.complete_operation.assert_awaited_once_with(5)
    journal.fail_operation.assert_not_awaited()


async def test_p1_journal_registra_el_veredicto_no_certificado(tmp_path: pathlib.Path) -> None:
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        success=True,
        verdict="completed_unverified",
        certificado=False,
        verdict_razones=("fallos_de_celda_historicos",),
        alcance_certificado="ninguno",
        evidencia={"worldspaces_solicitados": ["Tamriel"]},
    )
    service, _ = _servicio(tmp_path, journal=journal, runner_result=runner_result)

    resultado = await service.generate(_PAYLOAD)

    entradas = _entradas_de_veredicto(journal)
    assert len(entradas) == 1
    assert entradas[0]["metadata"]["certificado"] is False
    assert entradas[0]["metadata"]["razones"] == ["fallos_de_celda_historicos"]
    journal.fail_operation.assert_awaited_once()
    # La TX se commitea igual: el producto existe aunque no se certifique.
    journal.commit_transaction.assert_awaited_once_with(77)
    assert resultado["certificado"] is False


async def test_p1_el_evento_completed_lleva_el_veredicto(tmp_path: pathlib.Path) -> None:
    runner_result = _resultado_runner(
        verdict="completed_unverified",
        certificado=False,
        alcance_certificado="trabajo_de_la_corrida",
    )
    service, colab = _servicio(tmp_path, journal=_journal_con_loot_completado(), runner_result=runner_result)
    bus = colab["event_bus"]

    await service.generate(_PAYLOAD)

    payloads = [
        llamada.args[0].payload
        for llamada in bus.publish.await_args_list
        if getattr(llamada.args[0], "topic", None) == "pipeline.grass_cache.completed"
    ]
    assert payloads, "no se publicó pipeline.grass_cache.completed"
    assert payloads[-1]["verdict"] == "completed_unverified"
    assert payloads[-1]["certificado"] is False
    assert payloads[-1]["alcance_certificado"] == "trabajo_de_la_corrida"


# ---------------------------------------------------------------------------
# Revisión independiente: H6 — el servicio reconcilia la certificación
# ---------------------------------------------------------------------------


class _LockQuePierdeLaLease:
    """Async CM que simula perder la exclusividad al salir (después del runner)."""

    def __init__(self) -> None:
        self.rollback_completed = False

    async def __aenter__(self) -> _LockQuePierdeLaLease:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        raise RuntimeError("lease perdido al liberar el lock")


async def test_h6_perdida_de_lease_invalida_la_certificacion(tmp_path: pathlib.Path) -> None:
    """H6: si la operación pierde exclusividad DESPUÉS del runner, no se publica certificación."""
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        success=True,
        verdict="completed_verified",
        certificado=True,
        alcance_certificado="corrida_completa",
        evidencia={"worldspaces_solicitados": ["Tamriel"]},
    )
    service, colab = _servicio(
        tmp_path,
        journal=journal,
        runner_result=runner_result,
        lock_tx=_LockQuePierdeLaLease(),
        lock_txs={"load-order": _lock_tx_fake()},
    )

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is False
    assert resultado["certificado"] is False
    assert resultado["verdict"] == "completed_unverified"
    assert "operacion_invalidada" in resultado["verdict_razones"]
    assert resultado["alcance_certificado"] == "ninguno"
    # El journal registra el veredicto RECONCILIADO, no el del runner.
    entradas = _entradas_de_veredicto(journal)
    assert len(entradas) == 1
    assert entradas[0]["metadata"]["certificado"] is False
    assert "operacion_invalidada" in entradas[0]["metadata"]["razones"]
    # El evento tampoco publica certificación.
    payloads = [
        llamada.args[0].payload
        for llamada in colab["event_bus"].publish.await_args_list
        if getattr(llamada.args[0], "topic", None) == "pipeline.grass_cache.completed"
    ]
    assert payloads and payloads[-1]["certificado"] is False


async def test_h6_limpieza_incompleta_invalida_la_certificacion(tmp_path: pathlib.Path) -> None:
    """H6: un flag residual invalida la certificación aunque el runner haya certificado."""
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        success=True,
        verdict="completed_verified",
        certificado=True,
        alcance_certificado="corrida_completa",
        flag_residual=True,
        limpieza_errores=("unlink falló",),
        evidencia={"worldspaces_solicitados": ["Tamriel"]},
    )
    service, _ = _servicio(tmp_path, journal=journal, runner_result=runner_result)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is False
    assert resultado["certificado"] is False
    assert resultado["verdict"] == "completed_unverified"
    assert "limpieza_incompleta" in resultado["verdict_razones"]
    entradas = _entradas_de_veredicto(journal)
    assert entradas[0]["metadata"]["certificado"] is False


async def test_h6_certificacion_valida_se_mantiene(tmp_path: pathlib.Path) -> None:
    """Contraprueba: sin invalidación, la certificación sobrevive intacta."""
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        success=True,
        verdict="completed_verified",
        certificado=True,
        alcance_certificado="corrida_completa",
        evidencia={"worldspaces_solicitados": ["Tamriel"]},
    )
    service, _ = _servicio(tmp_path, journal=journal, runner_result=runner_result)

    resultado = await service.generate(_PAYLOAD)

    assert resultado["success"] is True
    assert resultado["certificado"] is True
    assert resultado["verdict"] == "completed_verified"
    assert _entradas_de_veredicto(journal)[0]["metadata"]["certificado"] is True


async def test_h2_terminalidad_no_verificada_omite_el_teardown(tmp_path: pathlib.Path) -> None:
    """H2/H5: con el juego posiblemente vivo NO se destruye el clon/mod (se preserva y se reporta)."""
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        success=False,
        verdict="unknown",
        certificado=False,
        verdict_razones=("terminalidad_no_verificada",),
        evidencia={"terminalidad": "vivo", "lanzamiento": "atribuido"},
    )
    service, colab = _servicio(tmp_path, journal=journal, runner_result=runner_result)

    resultado = await service.generate(_PAYLOAD)

    colab["profile_manager"].teardown.assert_not_awaited()
    assert any("teardown del clon/mod OMITIDO" in fallo for fallo in resultado["teardown_failures"])
    assert resultado["certificado"] is False
    journal.commit_transaction.assert_not_awaited()
    journal.mark_transaction_rolled_back.assert_awaited_once_with(77)


async def test_h2_terminalidad_demostrada_hace_teardown_normal(tmp_path: pathlib.Path) -> None:
    """Contraprueba: con terminalidad demostrada el teardown sigue ocurriendo."""
    journal = _journal_con_loot_completado()
    runner_result = _resultado_runner(
        success=True,
        verdict="completed_verified",
        certificado=True,
        alcance_certificado="corrida_completa",
        evidencia={"terminalidad": "terminado", "lanzamiento": "atribuido"},
    )
    service, colab = _servicio(tmp_path, journal=journal, runner_result=runner_result)

    await service.generate(_PAYLOAD)

    colab["profile_manager"].teardown.assert_awaited_once_with()
