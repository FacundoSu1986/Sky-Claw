"""Tests del Follow-up A — PandoraPipelineService (cobertura de lock).

Ancla el contrato de que la generación de animaciones de Pandora corre bajo el lock
distribuido compartido (``SnapshotTransactionLock``), serializándola contra otras
corridas concurrentes. Espeja el estilo de fixtures de ``test_loot_service.py``.
Como la salida de Pandora es dependiente del entorno (subproceso con ``cwd``), el
snapshot se difiere (``target_files=[]``) — la protección que aplica con certeza es la
serialización, igual que en ``LootSortingService``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.pandora_runner import PandoraExecutionError, PandoraResult
from sky_claw.local.tools.pandora_service import (
    BEHAVIOR_GRAPHS_RESOURCE_ID,
    PandoraPipelineService,
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


def _runner_returning(result: PandoraResult | None = None) -> MagicMock:
    runner = MagicMock()
    runner.run_pandora = AsyncMock(
        return_value=result or PandoraResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)
    )
    return runner


def _make_service(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
) -> PandoraPipelineService:
    return PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        pandora_runner=runner,
    )


@pytest.mark.asyncio
async def test_run_returns_success(lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager) -> None:
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is True
    assert result["return_code"] == 0
    runner.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_holds_lock_during_run(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Mientras Pandora corre, el lock de behavior-graphs lo tiene este servicio."""
    seen: dict[str, object] = {}

    async def on_run() -> PandoraResult:
        seen["info"] = await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID)
        return PandoraResult(success=True, return_code=0, stdout="", stderr="", duration_seconds=0.1)

    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=on_run)
    svc = _make_service(lock_manager, snapshot_manager, runner)

    await svc.generate_animations()

    info = seen["info"]
    assert info is not None
    assert info.agent_id == PandoraPipelineService.AGENT_ID  # type: ignore[attr-defined]
    # Lock liberado al salir del context transaccional.
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_serializes_when_lock_already_held(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un holder en competencia del lock bloquea la corrida (serialización)."""
    await lock_manager.acquire_lock(BEHAVIOR_GRAPHS_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert "lock" in result["logs"].lower()
    runner.run_pandora.assert_not_awaited()  # nunca corrió — no se pudo tomar el lock


@pytest.mark.asyncio
async def test_releases_lock_on_runner_failure(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Si Pandora lanza a mitad de corrida, el lock igual se libera (sin leak)."""
    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=PandoraExecutionError("boom"))
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_unsuccessful_result_maps_to_error_status(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un PandoraResult con success=False (p.ej. timeout) → status error."""
    runner = _runner_returning(
        PandoraResult(success=False, return_code=-1, stdout="", stderr="timeout", duration_seconds=2.0)
    )
    svc = _make_service(lock_manager, snapshot_manager, runner)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_builds_runner_from_resolver(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """Sin runner inyectado, el servicio resuelve el Pandora exe + game path."""
    pandora_exe = tmp_path / "Pandora.exe"
    pandora_exe.touch()
    game_path = tmp_path / "Skyrim"
    game_path.mkdir()

    resolver = MagicMock()
    resolver.get_pandora_exe = MagicMock(return_value=pandora_exe)
    resolver.get_skyrim_path = MagicMock(return_value=game_path)

    captured: dict[str, object] = {}

    class _FakeRunner:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        async def run_pandora(self) -> PandoraResult:
            return PandoraResult(success=True, return_code=0, stdout="", stderr="", duration_seconds=0.1)

    svc = PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
    )
    with patch("sky_claw.local.tools.pandora_service.PandoraRunner", _FakeRunner):
        result = await svc.generate_animations()

    assert result["success"] is True
    cfg = captured["config"]
    assert cfg.pandora_exe == pandora_exe  # type: ignore[attr-defined]
    assert cfg.game_path == game_path  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_paths_returns_error_without_locking(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Sin Pandora exe / game path resueltos → error dict, sin tomar el lock."""
    resolver = MagicMock()
    resolver.get_pandora_exe = MagicMock(return_value=None)
    resolver.get_skyrim_path = MagicMock(return_value=None)
    svc = PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=resolver,
    )

    result = await svc.generate_animations()

    assert result["success"] is False
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


# =============================================================================
# T-16c·4: gate de preflight en Pandora (antes de regenerar los behavior graphs)
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


def _perm_report(status: PreflightStatus, summary: str) -> PreflightReport:
    """Reporte con un solo check de permisos (el failure mode típico de Pandora:
    el dir de salida de behaviors sin permisos) en el estado pedido."""
    return PreflightReport(
        status=status,
        checks=(PreflightCheck(name="write_permissions", status=status, summary=summary, details=()),),
    )


def _svc_with_preflight(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
    preflight: object,
) -> PandoraPipelineService:
    return PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        pandora_runner=runner,
        preflight=preflight,  # type: ignore[arg-type]  # fake duck-typed en tests
    )


@pytest.mark.asyncio
async def test_preflight_red_blocks_pandora_without_running(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un preflight ROJO (p. ej. el dir de salida sin permisos) frena Pandora ANTES
    de tocar nada: no corre el subproceso, no toma el lock."""
    runner = _runner_returning()
    red = _perm_report(PreflightStatus.RED, "Data/overwrite sin permisos de escritura.")
    svc = _svc_with_preflight(lock_manager, snapshot_manager, runner, _FakePreflight(red))

    result = await svc.generate_animations()

    assert result["success"] is False
    assert result["reason"] == "PreflightBlocked"
    assert result["preflight"]["status"] == "red"
    runner.run_pandora.assert_not_awaited()
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_preflight_yellow_no_bloquea_y_surface(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un preflight AMARILLO no bloquea, pero se adjunta al result para el panel."""
    runner = _runner_returning()
    yellow = _perm_report(PreflightStatus.YELLOW, "Overwrite con residuos.")
    svc = _svc_with_preflight(lock_manager, snapshot_manager, runner, _FakePreflight(yellow))

    result = await svc.generate_animations()

    assert result["success"] is True
    assert result["preflight"]["status"] == "yellow"
    runner.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_green_no_ensucia_el_result(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Un preflight VERDE no agrega la clave ``preflight`` (comportamiento actual intacto)."""
    runner = _runner_returning()
    green = _perm_report(PreflightStatus.GREEN, "Escritura verificada.")
    svc = _svc_with_preflight(lock_manager, snapshot_manager, runner, _FakePreflight(green))

    result = await svc.generate_animations()

    assert result["success"] is True
    assert "preflight" not in result


@pytest.mark.asyncio
async def test_ensure_preflight_construye_sensores_con_paths_resolubles(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """Con game/MO2/exe resolubles, ``_ensure_preflight`` arma un PreflightService
    real (no None) y sondea los dirs candidatos de salida (Data/overwrite/exe)."""
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    (mo2 / "overwrite").mkdir(parents=True)
    exe = tmp_path / "Pandora" / "Pandora.exe"
    exe.parent.mkdir(parents=True)

    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    resolver.get_pandora_exe = MagicMock(return_value=exe)
    resolver.get_skyrim_path_raw = MagicMock(return_value=game)
    resolver.get_mo2_path_raw = MagicMock(return_value=mo2)

    svc = PandoraPipelineService(lock_manager=lock_manager, snapshot_manager=snapshot_manager, path_resolver=resolver)

    assert svc._ensure_preflight() is not None
    targets = svc._permission_targets()
    assert game / "Data" in targets
    assert mo2 / "overwrite" in targets
    assert exe.parent in targets


def test_permission_targets_incluye_pandora_output_concreto(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """Freshness/F2 (review #314): sondea el dir del exe Y el Pandora_Output hijo —
    un output read-only con el padre escribible pasaría inadvertido si no."""
    exe = tmp_path / "Pandora" / "Pandora.exe"
    exe.parent.mkdir(parents=True)
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=None)
    resolver.get_mo2_path = MagicMock(return_value=None)
    resolver.get_pandora_exe = MagicMock(return_value=exe)

    svc = PandoraPipelineService(lock_manager=lock_manager, snapshot_manager=snapshot_manager, path_resolver=resolver)
    targets = svc._permission_targets()

    assert exe.parent in targets
    assert exe.parent / "Pandora_Output" in targets


def test_preflight_con_runner_inyectado_sin_resolver(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """F1 (review #314): el agent tool construye el servicio con un PandoraRunner
    pero SIN resolver — el gate NO debe desactivarse; se deriva del config del runner."""
    from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraRunner

    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    exe = tmp_path / "Pandora" / "Pandora.exe"
    exe.parent.mkdir(parents=True)
    runner = PandoraRunner(PandoraConfig(pandora_exe=exe, game_path=game))

    svc = PandoraPipelineService(lock_manager=lock_manager, snapshot_manager=snapshot_manager, pandora_runner=runner)

    assert svc._ensure_preflight() is not None  # gate activo pese a no haber resolver
    targets = svc._permission_targets()
    assert game / "Data" in targets
    assert exe.parent in targets


def test_preflight_standalone_sin_mo2(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """F3 (review #314): standalone con SKYRIM_PATH/PANDORA_EXE pero sin MO2_PATH —
    el gate igual protege Data + el output del exe; solo se omite el sensor de overwrite."""
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    exe = tmp_path / "Pandora" / "Pandora.exe"
    exe.parent.mkdir(parents=True)
    resolver = MagicMock()
    resolver.get_skyrim_path = MagicMock(return_value=game)
    resolver.get_mo2_path = MagicMock(return_value=None)  # sin MO2
    resolver.get_pandora_exe = MagicMock(return_value=exe)
    resolver.get_skyrim_path_raw = MagicMock(return_value=game)
    resolver.get_mo2_path_raw = MagicMock(return_value=None)

    svc = PandoraPipelineService(lock_manager=lock_manager, snapshot_manager=snapshot_manager, path_resolver=resolver)

    assert svc._ensure_preflight() is not None  # no exige MO2
    targets = svc._permission_targets()
    assert game / "Data" in targets
    assert exe.parent in targets
    assert not any("overwrite" in str(t) for t in targets)  # sin MO2 → sin overwrite


# =============================================================================
# T-26/T-28 (ADR 0002): caja negra de vuelo — ActionManifest + FlightReport.
# Espeja la disciplina de loot_service (journal OPCIONAL: el path del agente
# construye el servicio sin journal y NO emite; el path GUI/dispatcher lo cablea).
# =============================================================================


@pytest.fixture
def mock_journal() -> AsyncMock:
    """Journal mockeado: begin_transaction devuelve un tx id fijo (77)."""
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=77)
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
) -> PandoraPipelineService:
    return PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        pandora_runner=runner,
        journal=journal,
    )


@pytest.mark.asyncio
async def test_emite_manifest_antes_de_correr_y_flight_report_tras_commit(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """T-26/T-28: con journal cableado, el manifiesto se persiste ANTES de correr
    Pandora y el informe de vuelo tras el commit (éxito)."""
    orden: list[str] = []

    async def _persist_manifest(*_a: object, **_k: object) -> None:
        orden.append("manifest")

    async def on_run() -> PandoraResult:
        orden.append("run")
        return PandoraResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=0.1)

    mock_journal.persist_action_manifest = AsyncMock(side_effect=_persist_manifest)
    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=on_run)
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with patch(
        "sky_claw.antigravity.orchestrator.preview.flight_report.compose_flight_report_from_journal",
        AsyncMock(return_value=MagicMock()),
    ):
        result = await svc.generate_animations()

    assert result["success"] is True
    assert orden == ["manifest", "run"]  # caja negra ANTES de mutar
    mock_journal.begin_transaction.assert_awaited_once()
    mock_journal.commit_transaction.assert_awaited_once_with(77)
    mock_journal.persist_flight_report.assert_awaited_once()
    mock_journal.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_fail_closed_aborta_sin_correr_pandora(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """T-26 fail-closed: si el manifiesto no se puede persistir, Pandora NO corre —
    la caja negra no es opcional cuando el journal está cableado."""
    mock_journal.persist_action_manifest = AsyncMock(side_effect=RuntimeError("journal DB locked"))
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert result["reason"] == "ActionManifestFailed"
    runner.run_pandora.assert_not_awaited()  # no mutó
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)  # TX no queda PENDING
    mock_journal.commit_transaction.assert_not_called()
    # El lock se liberó pese al abort.
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


@pytest.mark.asyncio
async def test_sin_journal_no_emite_manifest_pero_corre(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    """Path del agente (sin journal): Pandora corre normal, sin caja negra
    (honesto — no hay journal cableado que emitir)."""
    runner = _runner_returning()
    svc = _make_service(lock_manager, snapshot_manager, runner)  # sin journal

    result = await svc.generate_animations()

    assert result["success"] is True
    runner.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_flight_report_best_effort_no_rompe_el_exito(
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
        result = await svc.generate_animations()

    assert result["success"] is True  # el informe no rompe el contrato
    mock_journal.commit_transaction.assert_awaited_once_with(77)


@pytest.mark.asyncio
async def test_fallo_de_ejecucion_marca_la_tx_rolled_back(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Un PandoraExecutionError tras emitir el manifiesto marca la TX rolled-back
    (no queda PENDING) y no commitea."""
    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=PandoraExecutionError("boom"))
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.generate_animations()

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    mock_journal.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_resultado_non_zero_marca_la_tx_rolled_back(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Un PandoraResult con success=False (timeout/exit non-zero) también cierra
    la TX como rolled-back en vez de dejarla PENDING."""
    runner = _runner_returning(
        PandoraResult(success=False, return_code=-1, stdout="", stderr="timeout", duration_seconds=2.0)
    )
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.generate_animations()

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    mock_journal.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_lock_contention_no_abre_transaccion(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """La emisión del manifiesto ocurre DENTRO del lock: si no se pudo tomar,
    el journal ni se toca."""
    await lock_manager.acquire_lock(BEHAVIOR_GRAPHS_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.generate_animations()

    assert result["success"] is False
    mock_journal.begin_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()


@pytest.mark.asyncio
async def test_manifest_rollback_falla_igual_devuelve_dict(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Si marcar la TX rolled-back falla tras el fallo del manifiesto, el servicio
    igual devuelve un dict serializable (no propaga — contrato T11)."""
    mock_journal.persist_action_manifest = AsyncMock(side_effect=RuntimeError("persist falló"))
    mock_journal.mark_transaction_rolled_back = AsyncMock(side_effect=OSError("journal DB locked"))
    runner = _runner_returning()
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert result["reason"] == "ActionManifestFailed"
    runner.run_pandora.assert_not_awaited()
