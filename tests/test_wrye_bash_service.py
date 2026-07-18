"""Tests de la extracción Strangler-Fig — WryeBashPipelineService (PR A).

Ancla el contrato de que la generación del Bashed Patch de Wrye Bash corre bajo el
lock distribuido compartido (``SnapshotTransactionLock``), serializándola contra
otras corridas concurrentes. Wrye Bash era el único ritual mutante que NO estaba
serializado (su lógica vivía en ``SupervisorAgent.execute_wrye_bash_pipeline`` sin
lock); este servicio cierra ese hueco.

Espeja el estilo de fixtures de ``test_pandora_service.py``. Como la salida de Wrye
Bash es dependiente del entorno (subproceso con ``cwd`` que escribe vía la VFS de
MO2), el snapshot se difiere (``target_files=[]``) — la protección que aplica con
certeza es la serialización. El guard M-04 (compartido) se INYECTA desde el
supervisor, no lo posee este servicio.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager, LockLeaseLostError
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.wrye_bash_runner import WryeBashExecutionError, WryeBashResult
from sky_claw.local.tools.wrye_bash_service import (
    BASHED_PATCH_RESOURCE_ID,
    WryeBashPipelineService,
)

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
def tmp_lock_db(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "test_locks.db"


@pytest.fixture
async def lock_manager(tmp_lock_db: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_lock_db,
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
    d = tmp_path / "snapshots"
    d.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=d)
    await mgr.initialize()
    return mgr


def _runner_returning(result: WryeBashResult | None = None) -> MagicMock:
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(
        return_value=result or WryeBashResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)
    )
    return runner


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    *,
    plugin_limit_guard: Any | None = None,
) -> WryeBashPipelineService:
    return WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        wrye_bash_runner=runner,
        plugin_limit_guard=plugin_limit_guard,
    )


@pytest.mark.asyncio
async def test_genera_bashed_patch_exitoso(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    assert result["return_code"] == 0
    assert result["stdout"] == "ok"
    runner.generate_bashed_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_toma_el_lock_durante_la_corrida(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Mientras Wrye Bash corre, el lock del Bashed Patch lo tiene este servicio."""
    seen: dict[str, object] = {}

    async def on_run() -> WryeBashResult:
        seen["info"] = await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID)
        return WryeBashResult(success=True, return_code=0, stdout="", stderr="", duration_seconds=0.1)

    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    svc = _make_service(lock_manager, snapshot_manager, runner)

    await svc.execute_pipeline(profile="Default")

    info = seen["info"]
    assert info is not None
    assert info.agent_id == WryeBashPipelineService.AGENT_ID  # type: ignore[attr-defined]
    # Lock liberado al salir del context transaccional.
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_serializa_cuando_el_lock_ya_esta_tomado(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un holder en competencia del lock bloquea la corrida (serialización)."""
    await lock_manager.acquire_lock(BASHED_PATCH_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert "lock" in result["error"].lower()
    runner.generate_bashed_patch.assert_not_awaited()  # nunca corrió — no se pudo tomar el lock


@pytest.mark.asyncio
async def test_libera_lock_ante_fallo_del_runner(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Si Wrye Bash lanza a mitad de corrida, el lock igual se libera (sin leak)."""
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(side_effect=WryeBashExecutionError("boom"))
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert "boom" in result["error"]
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_guard_m04_falla_aborta_sin_correr(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """El guard M-04 inyectado en rojo aborta ANTES de tocar el runner o el lock."""
    guard = AsyncMock(return_value={"valid": False, "error": "254 excedido", "plugin_count": 300})
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner, plugin_limit_guard=guard)

    result = await svc.execute_pipeline(profile="MiPerfil")

    assert result["success"] is False
    assert result["aborted_by"] == "plugin_limit_guard"
    assert result["plugin_count"] == 300
    assert result["error"] == "254 excedido"
    guard.assert_awaited_once_with("MiPerfil")
    runner.generate_bashed_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_m04_valido_corre(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    guard = AsyncMock(return_value={"valid": True, "plugin_count": 100})
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner, plugin_limit_guard=guard)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    guard.assert_awaited_once_with("Default")
    runner.generate_bashed_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_limit_false_saltea_el_guard(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    guard = AsyncMock(return_value={"valid": False, "error": "no debería llamarse"})
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner, plugin_limit_guard=guard)

    result = await svc.execute_pipeline(profile="Default", validate_limit=False)

    assert result["success"] is True
    guard.assert_not_awaited()
    runner.generate_bashed_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_no_disponible_devuelve_error(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Sin runner ni path_resolver, ensure_runner lanza y execute_pipeline lo captura."""
    svc = WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=None,
        wrye_bash_runner=None,
    )

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_lock_contention_devuelve_error(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Contención de lock (LockAcquisitionError) → dict de error serializable."""
    from sky_claw.antigravity.db.locks import LockAcquisitionError

    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    with patch(
        "sky_claw.local.tools.wrye_bash_service.SnapshotTransactionLock",
        side_effect=LockAcquisitionError(BASHED_PATCH_RESOURCE_ID, "other", "busy"),
    ):
        result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert "lock" in result["error"].lower()
    runner.generate_bashed_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_runner_construye_desde_el_resolver(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Sin runner inyectado, el servicio resuelve bash + game + mo2 desde el resolver."""
    wrye_bash_exe = tmp_path / "bash.exe"
    wrye_bash_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()
    mo2_path = tmp_path / "MO2"
    mo2_path.mkdir()

    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game_path)
    resolver.get_mo2_path = MagicMock(return_value=mo2_path)
    resolver.get_wrye_bash_path = MagicMock(return_value=wrye_bash_exe)

    svc = WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
    )

    runner = svc.ensure_runner()

    assert runner.config.wrye_bash_path == wrye_bash_exe
    assert runner.config.game_path == game_path
    assert runner.config.mo2_path == mo2_path


# ---------------------------------------------------------------------------
# Review Codex #315 — lock anidado con load-order, lease perdido, message canónico
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toma_tambien_el_lock_de_load_order(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """El lock anidado toma load-order además del Bashed Patch (invariante post-LOOT)."""
    from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID

    seen: dict[str, object] = {}

    async def on_run() -> WryeBashResult:
        seen["bashed"] = await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID)
        seen["load_order"] = await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID)
        return WryeBashResult(success=True, return_code=0, stdout="", stderr="", duration_seconds=0.1)

    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    svc = _make_service(lock_manager, snapshot_manager, runner)

    await svc.execute_pipeline(profile="Default")

    assert seen["bashed"] is not None
    assert seen["load_order"] is not None
    # Ambos liberados al salir del context anidado.
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None
    assert await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_serializa_contra_sort_de_loot(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un LOOT sort en curso (load-order tomado) bloquea a Wrye Bash — no corre."""
    from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID

    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "loot-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert "lock" in result["error"].lower()
    runner.generate_bashed_patch.assert_not_awaited()
    # El lock externo (Bashed Patch) NO quedó colgado tras fallar la adquisición interna.
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_lease_perdido_al_cerrar_devuelve_error(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un LockLeaseLostError en __aexit__ (renovación fallida) no crashea el dispatch."""
    from sky_claw.antigravity.db.locks import LockLeaseLostError

    class _LeaseLostLock:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _LeaseLostLock:
            return self

        async def __aexit__(self, *exc: object) -> None:
            raise LockLeaseLostError("lease perdido durante la corrida")

    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    with patch("sky_claw.local.tools.wrye_bash_service.SnapshotTransactionLock", _LeaseLostLock):
        result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert result["message"]  # detalle serializable, no un crash de dispatch
    runner.generate_bashed_patch.assert_awaited_once()  # el runner corrió; el lease se perdió al cerrar


@pytest.mark.asyncio
async def test_fallo_non_zero_emite_message_canonico(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un exit non-zero llena el campo canónico ``message`` con el stderr (contrato de tools)."""
    runner = _runner_returning(
        WryeBashResult(
            success=False, return_code=2, stdout="", stderr="patch failed: master missing", duration_seconds=1.0
        )
    )
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert result["message"] == "patch failed: master missing"


@pytest.mark.asyncio
async def test_exito_message_vacio(lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager) -> None:
    """En éxito, ``message`` es cadena vacía (canónico)."""
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    assert result["message"] == ""


# =============================================================================
# PR B (T-16c) — gate de preflight antes de generar el Bashed Patch
# =============================================================================

from sky_claw.local.validators.preflight import (  # noqa: E402
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
)


class _FakePreflight:
    """Preflight inyectable: ``run()`` devuelve un reporte fijo."""

    def __init__(self, report: PreflightReport) -> None:
        self._report = report

    async def run(self) -> PreflightReport:
        return self._report


def _report(status: PreflightStatus, summary: str) -> PreflightReport:
    """Reporte con un solo check (p. ej. permisos sobre el destino del Bashed Patch)."""
    return PreflightReport(
        status=status,
        checks=(PreflightCheck(name="write_permissions", status=status, summary=summary, details=()),),
    )


def _svc_with_preflight(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    preflight: object,
) -> WryeBashPipelineService:
    return WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        wrye_bash_runner=runner,
        preflight=preflight,  # type: ignore[arg-type]  # fake duck-typed en tests
    )


@pytest.mark.asyncio
async def test_preflight_red_bloquea_sin_correr(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un preflight ROJO (destino del Bashed Patch sin permisos / master faltante) frena
    Wrye Bash ANTES de tocar nada: no corre el subproceso ni toma el lock."""
    runner = _runner_returning()
    red = _report(PreflightStatus.RED, "Data/overwrite sin permisos de escritura.")
    svc = _svc_with_preflight(lock_manager, snapshot_manager, runner, _FakePreflight(red))

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert result["reason"] == "PreflightBlocked"
    assert result["preflight"]["status"] == "red"
    runner.generate_bashed_patch.assert_not_awaited()
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_preflight_red_no_llama_al_guard_m04(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """El gate rojo corta ANTES del guard M-04 (preflight brutal primero)."""
    runner = _runner_returning()
    guard = AsyncMock(return_value={"valid": True})
    red = _report(PreflightStatus.RED, "master faltante")
    svc = WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        wrye_bash_runner=runner,
        plugin_limit_guard=guard,
        preflight=_FakePreflight(red),  # type: ignore[arg-type]
    )

    result = await svc.execute_pipeline(profile="Default")

    assert result["reason"] == "PreflightBlocked"
    guard.assert_not_awaited()
    runner.generate_bashed_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_yellow_no_bloquea_pero_surface(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Amarillo (p. ej. overwrite sucio) NO bloquea; el reporte viaja en el result."""
    runner = _runner_returning()
    yellow = _report(PreflightStatus.YELLOW, "overwrite con residuos.")
    svc = _svc_with_preflight(lock_manager, snapshot_manager, runner, _FakePreflight(yellow))

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    assert result["preflight"]["status"] == "yellow"
    runner.generate_bashed_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_green_no_ensucia_el_result(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Verde: no bloquea y NO adjunta la clave ``preflight`` (mismo criterio que hermanos)."""
    runner = _runner_returning()
    green = _report(PreflightStatus.GREEN, "Escritura verificada.")
    svc = _svc_with_preflight(lock_manager, snapshot_manager, runner, _FakePreflight(green))

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    assert "preflight" not in result
    runner.generate_bashed_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_sin_fuentes_resolubles_no_gatea(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Sin game/MO2 resoluble (resolver devuelve no-Path), _ensure_preflight → None: sin gate."""
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=None)
    resolver.get_mo2_path = MagicMock(return_value=None)
    runner = _runner_returning()
    svc = WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
        wrye_bash_runner=runner,
    )

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    assert "preflight" not in result
    runner.generate_bashed_patch.assert_awaited_once()


# =============================================================================
# T-26/T-28 (ADR 0002, "PR C"): caja negra de vuelo — ActionManifest + FlightReport.
# Wrye Bash era el ÚLTIMO ritual mutante sin caja negra (6/6 con esto). Espeja
# loot_service/pandora_service: journal OPCIONAL, cableado en AMBOS paths de
# producción (GUI y agente) vía app_context. Convive con el preflight (PR B).
# =============================================================================


@pytest.fixture
def mock_journal() -> AsyncMock:
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=88)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    journal.persist_action_manifest = AsyncMock()
    journal.persist_flight_report = AsyncMock()
    return journal


def _svc_with_journal(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    journal: AsyncMock,
) -> WryeBashPipelineService:
    return WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        wrye_bash_runner=runner,
        journal=journal,
    )


@pytest.mark.asyncio
async def test_cn_emite_manifest_antes_de_correr_y_flight_report_tras_commit(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """T-26/T-28: con journal cableado, el manifiesto se persiste ANTES de correr
    Wrye Bash y el informe de vuelo tras el commit (éxito)."""
    orden: list[str] = []

    async def _persist_manifest(*_a: object, **_k: object) -> None:
        orden.append("manifest")

    async def on_run() -> WryeBashResult:
        orden.append("run")
        return WryeBashResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=0.1)

    mock_journal.persist_action_manifest = AsyncMock(side_effect=_persist_manifest)
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with patch(
        "sky_claw.antigravity.orchestrator.preview.flight_report.compose_flight_report_from_journal",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    assert orden == ["manifest", "run"]  # caja negra ANTES de mutar
    mock_journal.begin_transaction.assert_awaited_once()
    mock_journal.commit_transaction.assert_awaited_once_with(88)
    mock_journal.persist_flight_report.assert_awaited_once()
    mock_journal.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_cn_manifest_fail_closed_aborta_sin_correr(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """T-26 fail-closed: si el manifiesto no se puede persistir, Wrye Bash NO corre."""
    mock_journal.persist_action_manifest = AsyncMock(side_effect=RuntimeError("journal DB locked"))
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    assert result["reason"] == "ActionManifestFailed"
    runner.generate_bashed_patch.assert_not_awaited()
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(88)
    mock_journal.commit_transaction.assert_not_called()
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_cn_sin_journal_no_emite_pero_corre(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Sin journal (callers legacy / tests) corre normal sin emitir la caja negra."""
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)  # sin journal

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    runner.generate_bashed_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_cn_flight_report_best_effort_no_rompe_exito(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """T-28 best-effort: un fallo al persistir el informe NO tumba un run exitoso."""
    mock_journal.persist_flight_report = AsyncMock(side_effect=RuntimeError("journal caído"))
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with patch(
        "sky_claw.antigravity.orchestrator.preview.flight_report.compose_flight_report_from_journal",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is True
    mock_journal.commit_transaction.assert_awaited_once_with(88)


@pytest.mark.asyncio
async def test_cn_fallo_de_ejecucion_marca_rolled_back(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Un WryeBashExecutionError tras el manifiesto marca la TX rolled-back."""
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(side_effect=WryeBashExecutionError("boom"))
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(88)
    mock_journal.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_cn_resultado_non_zero_marca_rolled_back(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Un WryeBashResult con success=False (exit non-zero) cierra la TX rolled-back."""
    runner = _runner_returning(
        WryeBashResult(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=1.0)
    )
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(88)
    mock_journal.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_cn_lock_contention_no_abre_transaccion(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """La emisión del manifiesto ocurre DENTRO del lock: si no se pudo tomar, el
    journal ni se toca."""
    await lock_manager.acquire_lock(BASHED_PATCH_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    mock_journal.begin_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()


class _LockLeaseLostFake:
    """Lock de frontera: en un clean-exit ``__aexit__`` se comporta como lease perdida
    (review Codex #318). ``snapshots=[]`` porque Wrye Bash corre con snapshot diferido."""

    snapshots: list[object] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _LockLeaseLostFake:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: object, tb: object) -> bool:
        if exc_type is None:
            raise LockLeaseLostError("lease perdida durante el run (simulado)")
        return False


@pytest.mark.asyncio
async def test_cn_lease_perdida_marca_rolled_back(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Con journal cableado, un LockLeaseLostError en el __aexit__ del lock cierra la
    TX rolled-back (no queda PENDING) además de devolver el dict de error."""
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with patch("sky_claw.local.tools.wrye_bash_service.SnapshotTransactionLock", _LockLeaseLostFake):
        result = await svc.execute_pipeline(profile="Default")

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(88)
    mock_journal.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_cn_cancelacion_marca_rolled_back_y_propaga(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Una cancelación (shutdown/timeout) mientras corre Wrye Bash cierra la TX
    rolled-back y re-lanza la CancelledError (review Codex #318)."""
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(side_effect=asyncio.CancelledError())
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with pytest.raises(asyncio.CancelledError):
        await svc.execute_pipeline(profile="Default")

    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(88)
    mock_journal.commit_transaction.assert_not_called()
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None
