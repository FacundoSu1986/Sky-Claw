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

from sky_claw.antigravity.db.journal import OperationStatus, TransactionStatus
from sky_claw.antigravity.db.locks import LockAcquisitionError
from sky_claw.local.mo2.grass_profile import GrassProfileError
from sky_claw.local.tools.grass_cache_runner import GrassCacheRunResult
from sky_claw.local.tools.grass_cache_service import (
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


def test_contrato_flight_report_lectura_escritura() -> None:
    """§2.2: el helper de lectura (is_flight_report_committed) concuerda con lo
    que escribe el modelo FlightReport de LOOT — mismo kind y transaction_status."""
    from sky_claw.antigravity.db.journal import TransactionStatus
    from sky_claw.antigravity.db.journal_contracts import FLIGHT_REPORT_KIND, is_flight_report_committed
    from sky_claw.antigravity.orchestrator.preview.manifest import FlightReport

    commiteado = FlightReport(transaction_status=TransactionStatus.COMMITTED.value).model_dump(mode="json")
    assert commiteado["kind"] == FLIGHT_REPORT_KIND
    assert is_flight_report_committed(commiteado) is True

    revertido = FlightReport(transaction_status=TransactionStatus.ROLLED_BACK.value).model_dump(mode="json")
    assert is_flight_report_committed(revertido) is False
    # Un ActionManifest (sin kind=flight_report) tampoco alcanza.
    assert is_flight_report_committed({"ritual_id": "loot-sort-1", "transaction_status": "committed"}) is False
    assert is_flight_report_committed(None) is False


async def test_generate_resuelve_deps_por_provider_lazy(tmp_path: pathlib.Path) -> None:
    """Las deps de Fases B/C pueden venir de un provider lazy (no solo concretas):
    se resuelven al ejecutar el ritual, tras la hidratación de entorno (Codex #301)."""
    game = tmp_path / "game"
    game.mkdir()
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    (tmp_path / "mo2" / "overwrite").mkdir(parents=True)

    profile_manager = AsyncMock()
    profile_manager.clone_profile = "SkyClaw-GrassCache"
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

    from sky_claw.antigravity.db.locks import LockInfo
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
    flag.write_text("", encoding="utf-8")
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
    flag.write_text("", encoding="utf-8")
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
    flag.write_text("", encoding="utf-8")
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
    flag.write_text("", encoding="utf-8")
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
