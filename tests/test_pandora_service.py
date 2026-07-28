"""Tests del Follow-up A — PandoraPipelineService (cobertura de lock).

Ancla el contrato de que la generación de animaciones de Pandora corre bajo el lock
distribuido compartido (``SnapshotTransactionLock``), serializándola contra otras
corridas concurrentes. Espeja el estilo de fixtures de ``test_loot_service.py``.
El snapshot se difiere (``target_files=[]``) y la protección que aplica hoy es la
serialización, igual que en ``LootSortingService``. **El motivo ya no es que la
salida sea "dependiente del entorno"** — U-01 parte 2 desmontó esa premisa: Pandora
se spawnea directo, no hereda la USVFS y escribe físicamente en las candidatas de
``output_targets.pandora_output_candidates``. Snapshotearlas es alcance de U-04.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.db.locks import DistributedLockManager, LockLeaseLostError
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
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
    real (no None) y sondea los dirs candidatos de salida FÍSICOS (Data/exe).

    El ``overwrite`` de MO2 salió del sondeo en U-01 parte 2: llegar ahí exige la
    redirección USVFS y Pandora se spawnea directo, así que sondearlo modelaba un
    modo de lanzamiento que no existe. Se afirma su AUSENCIA para que reponerlo
    tenga que pasar por este test.
    """
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
    assert mo2 / "overwrite" not in targets
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
        "sky_claw.app.orchestrator.preview.flight_report.compose_flight_report_from_journal",
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
    """Sin journal (callers legacy / tests), Pandora corre normal sin emitir la caja
    negra — el gating es por presencia del journal, no por path. En producción AMBOS
    paths (GUI y agente) lo cablean vía app_context (review Codex #318)."""
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
        "sky_claw.app.orchestrator.preview.flight_report.compose_flight_report_from_journal",
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


class _LeaseLostLock:
    """Lock de frontera: en un clean-exit (el body no lanzó) ``__aexit__`` se comporta
    como una lease perdida — mismo contrato que ``SnapshotTransactionLock`` real (review
    Codex #318: el heartbeat pudo perder el lease DURANTE el run aunque el body haya
    terminado con éxito). ``snapshots=[]`` porque Pandora corre con snapshot diferido."""

    snapshots: list[object] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _LeaseLostLock:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: object, tb: object) -> bool:
        if exc_type is None:
            raise LockLeaseLostError("lease perdida durante el run (simulado)")
        return False


@pytest.mark.asyncio
async def test_lease_perdida_devuelve_dict_y_marca_rolled_back(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Si el heartbeat pierde el lease en un clean-exit (LockLeaseLostError en
    __aexit__), generate_animations devuelve el dict de error (no propaga) y cierra la
    TX rolled-back (no queda PENDING) — la exclusividad no estuvo garantizada."""
    runner = _runner_returning()  # el run "termina con éxito" antes del __aexit__
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with patch("sky_claw.local.tools.pandora_service.SnapshotTransactionLock", _LeaseLostLock):
        result = await svc.generate_animations()

    assert result["success"] is False
    assert "lease" in result["message"].lower()
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    mock_journal.commit_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_cancelacion_marca_rolled_back_y_propaga(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, mock_journal: AsyncMock
) -> None:
    """Una cancelación (shutdown/timeout) mientras corre Pandora cierra la TX
    rolled-back (no PENDING) y re-lanza la CancelledError (review Codex #318)."""
    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=asyncio.CancelledError())
    svc = _svc_with_journal(lock_manager, snapshot_manager, runner, mock_journal)

    with pytest.raises(asyncio.CancelledError):
        await svc.generate_animations()

    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    mock_journal.commit_transaction.assert_not_called()
    # El lock se liberó pese a la cancelación.
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None


# =============================================================================
# U-04 — rollback REAL de la salida de Pandora (move-aside de ``Pandora_Output``).
#
# Cierra la mitad que #397 dejó abierta a propósito. La causa raíz es la MISMA que
# en Wrye Bash —los dos mecanismos de rollback del repo restauran sólo en la rama
# de excepción, y un ``PandoraResult(success=False)`` sale limpio del ``async
# with``—, pero la primitiva es distinta: Wrye Bash produce un archivo único
# (``target_files`` del lock) y Pandora un ÁRBOL de behavior graphs, así que acá va
# ``DirectoryRollback`` (move-aside O(1)), igual que DynDOLOD.
#
# Los tests usan archivos REALES en disco: contar llamadas a un mock probaría que
# se invocó el rollback, no que la salida volvió.
# =============================================================================


def _escribir_output(destino: pathlib.Path, contenido: str) -> None:
    """Materializa un árbol de salida de Pandora con contenido reconocible."""
    (destino / "meshes" / "actors").mkdir(parents=True, exist_ok=True)
    (destino / "meshes" / "actors" / "defaultmale.hkx").write_text(contenido, encoding="utf-8")


def _leer_output(destino: pathlib.Path) -> str:
    return (destino / "meshes" / "actors" / "defaultmale.hkx").read_text(encoding="utf-8")


def _svc_con_salida_real(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    *,
    game: pathlib.Path,
    exe: pathlib.Path,
    corrida: object,
    journal: AsyncMock | None = None,
) -> PandoraPipelineService:
    """Servicio con rutas REALES en disco y preflight verde inyectado.

    El runner es un mock cuyo ``run_pandora`` **escribe de verdad** en el árbol de
    salida antes de devolver/lanzar: es la única forma de afirmar que el rollback
    restauró byte a byte en vez de que simplemente se llamó.
    """
    from sky_claw.local.tools.pandora_runner import PandoraConfig

    runner = MagicMock()
    runner.config = PandoraConfig(pandora_exe=exe, game_path=game)
    runner.run_pandora = AsyncMock(side_effect=corrida)
    return PandoraPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        pandora_runner=runner,
        preflight=_FakePreflight(_perm_report(PreflightStatus.GREEN, "Escritura verificada.")),  # type: ignore[arg-type]
        journal=journal,
    )


def _rutas_de_salida(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, list[pathlib.Path]]:
    """``(game, exe, [las dos raíces de Pandora_Output])``, ya creadas en disco."""
    from sky_claw.local.tools.output_targets import PANDORA_OUTPUT_DIR

    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    exe = tmp_path / "Pandora" / "Pandora Behaviour Engine+.exe"
    exe.parent.mkdir(parents=True)
    return game, exe, [game / PANDORA_OUTPUT_DIR, exe.parent / PANDORA_OUTPUT_DIR]


@pytest.mark.asyncio
async def test_fallo_non_zero_restaura_las_dos_raices_de_salida(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """Un exit non-zero revierte el árbol de salida en AMBAS raíces.

    Se afirman las dos —la del ``cwd`` (``game_path``) y la del exe— en el mismo
    test a propósito: son hermanas exactas y cubrir sólo una es el defecto #1 del
    repo. Cuál de las dos usa Pandora depende de su versión, no del entorno.
    """
    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "bueno")

    async def _corre_y_falla() -> PandoraResult:
        for salida in salidas:
            _escribir_output(salida, "corrupto")  # parcial a medio escribir
        return PandoraResult(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=1.0)

    svc = _svc_con_salida_real(lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_y_falla)

    result = await svc.generate_animations()

    assert result["success"] is False
    assert result["return_code"] == 1  # el contrato del dict no cambia
    assert result["stderr"] == "boom"
    for salida in salidas:
        assert _leer_output(salida) == "bueno", f"la salida de {salida} no se restauró"


@pytest.mark.asyncio
async def test_timeout_restaura_la_salida_previa(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """El otro modo de fallo del ítem: ``PandoraTimeoutError`` (U-10) ya elevaba,
    así que con el move-aside cableado restaura sin código dedicado."""
    from sky_claw.local.tools.pandora_runner import PandoraTimeoutError

    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "bueno")

    async def _corre_y_cuelga() -> PandoraResult:
        for salida in salidas:
            _escribir_output(salida, "corrupto")
        raise PandoraTimeoutError(300.0)

    svc = _svc_con_salida_real(lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_y_cuelga)

    result = await svc.generate_animations()

    assert result["success"] is False
    for salida in salidas:
        assert _leer_output(salida) == "bueno", f"la salida de {salida} no se restauró tras el timeout"


@pytest.mark.asyncio
async def test_exito_conserva_la_salida_nueva_y_descarta_el_backup(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """El camino feliz no debe revertir nada ni dejar backups huérfanos ocupando
    disco (los behavior graphs pesan)."""
    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "viejo")

    async def _corre_ok() -> PandoraResult:
        for salida in salidas:
            _escribir_output(salida, "nuevo")
        return PandoraResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)

    svc = _svc_con_salida_real(lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_ok)

    result = await svc.generate_animations()

    assert result["success"] is True
    for salida in salidas:
        assert _leer_output(salida) == "nuevo"
        assert not list(salida.parent.glob(f"{salida.name}.rollback-*")), "quedó un backup huérfano"


@pytest.mark.asyncio
async def test_primer_run_fallido_borra_el_parcial(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """Sin salida previa, el move-aside igual revierte: borra el parcial.

    Es donde ``DirectoryRollback`` es ESTRICTAMENTE mejor que el snapshot del lock
    —la limitación aceptada de Wrye Bash en #397 (``file_path.exists()`` gatea el
    snapshot, así que un primer run fallido deja el parcial)— porque "volver al
    estado previo" cuando el estado previo era "no existía" es borrarlo, y eso no
    necesita backup.
    """
    game, exe, salidas = _rutas_de_salida(tmp_path)  # sin Pandora_Output previo

    async def _corre_y_falla() -> PandoraResult:
        for salida in salidas:
            _escribir_output(salida, "parcial")
        return PandoraResult(success=False, return_code=2, stdout="", stderr="crash", duration_seconds=1.0)

    svc = _svc_con_salida_real(lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_y_falla)

    result = await svc.generate_animations()

    assert result["success"] is False
    for salida in salidas:
        assert not salida.exists(), f"el parcial de {salida} quedó en disco"


@pytest.mark.asyncio
async def test_el_rollback_no_toca_el_data_del_juego_ni_el_dir_del_exe(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """La restricción de seguridad del mecanismo, afirmada end-to-end.

    ``DirectoryRollback`` RENOMBRA el directorio entero aparte. Eso sólo es correcto
    sobre un dir que la herramienta regenera por completo. El ``Data`` del juego
    contiene TODO el setup de mods del usuario y ``exe.parent`` contiene el
    ejecutable que está por correr: un move-aside ahí destruiría estado que Pandora
    no produjo, y sería un remedio peor que la enfermedad que U-04 describe.

    **Se observa DURANTE el run, no después.** Un move-aside indebido va y vuelve
    —renombra al entrar, restaura al fallar—, así que mirar el disco al final lo
    taparía por completo: verificado simulando la regresión (devolver todas las
    candidatas como revertibles) contra la versión que sólo miraba el estado final,
    y pasaba igual. El único instante en que el daño es visible es mientras Pandora
    corre, que además es exactamente cuando importa: si el ``Data`` está renombrado
    ahí, la herramienta lee un juego sin mods.
    """
    game, exe, salidas = _rutas_de_salida(tmp_path)
    (game / "Data" / "Skyrim.esm").write_text("master intocable", encoding="utf-8")
    exe.write_text("exe intocable", encoding="utf-8")
    durante: dict[str, bool] = {}

    async def _corre_y_falla() -> PandoraResult:
        durante["data_en_su_lugar"] = (game / "Data" / "Skyrim.esm").exists()
        durante["exe_en_su_lugar"] = exe.exists()
        for salida in salidas:
            _escribir_output(salida, "corrupto")
        return PandoraResult(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=1.0)

    svc = _svc_con_salida_real(lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_y_falla)

    await svc.generate_animations()

    assert durante["data_en_su_lugar"], "el Data del juego fue movido aparte mientras Pandora corría"
    assert durante["exe_en_su_lugar"], "el dir del exe fue movido aparte mientras Pandora corría"
    assert (game / "Data" / "Skyrim.esm").read_text(encoding="utf-8") == "master intocable"
    assert exe.read_text(encoding="utf-8") == "exe intocable"
    # Secundario: un restore fallido dejaría el backup huérfano a la vista.
    assert not list(tmp_path.rglob("Data.rollback-*"))
    assert not list(tmp_path.rglob("Pandora.rollback-*"))


@pytest.mark.asyncio
async def test_restore_fallido_deja_la_tx_pendiente(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    mock_journal: AsyncMock,
) -> None:
    """Si el restore falla (disco lleno), la TX NO puede cerrarse como revertida.

    ``DirectoryRollback`` se traga el ``OSError`` del restore para no enmascarar la
    excepción del body y lo expone vía ``rollback_completed=False``. Marcar la TX
    ``rolled_back`` igual escondería del recovery manual una salida parcial que
    sigue en disco — el mismo P1 que Codex encontró en #397.
    """
    from sky_claw.local.tools._dir_rollback import DirectoryRollback

    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "bueno")

    async def _corre_y_falla() -> PandoraResult:
        return PandoraResult(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=1.0)

    svc = _svc_con_salida_real(
        lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_y_falla, journal=mock_journal
    )

    with patch.object(DirectoryRollback, "_restore_backup", AsyncMock(side_effect=OSError("No space left on device"))):
        result = await svc.generate_animations()

    assert result["success"] is False
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()  # queda PENDING, a la vista


@pytest.mark.asyncio
async def test_fallo_con_journal_cierra_la_tx_tras_un_rollback_honesto(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    mock_journal: AsyncMock,
) -> None:
    """El hermano del test anterior: con el rollback COMPLETADO, la TX sí se cierra."""
    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "bueno")

    async def _corre_y_falla() -> PandoraResult:
        for salida in salidas:
            _escribir_output(salida, "corrupto")
        return PandoraResult(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=1.0)

    svc = _svc_con_salida_real(
        lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_y_falla, journal=mock_journal
    )

    result = await svc.generate_animations()

    assert result["success"] is False
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    mock_journal.commit_transaction.assert_not_called()
    for salida in salidas:
        assert _leer_output(salida) == "bueno"


@pytest.mark.asyncio
async def test_exito_no_borra_el_candidato_que_pandora_no_regenero(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """El candidato INACTIVO no puede desaparecer tras un run exitoso (review Codex #399).

    Las dos raíces de ``Pandora_Output`` son candidatas porque cuál usa Pandora
    depende de su versión: en una instalación real se escribe **una sola**. El
    move-aside entra en las dos, así que si el descarte del backup no mira si la
    herramienta regeneró el dir, el candidato que Pandora no tocó pierde su único
    ejemplar — borrado permanente, en el camino FELIZ, y sin excepción que lo
    delate.

    El test anterior (``test_exito_conserva_la_salida_nueva``) escribía en ambos y
    tapaba exactamente esto.
    """
    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "viejo")
    activa, inactiva = exe.parent / "Pandora_Output", game / "Pandora_Output"

    async def _corre_ok() -> PandoraResult:
        _escribir_output(activa, "nuevo")  # esta versión de Pandora usa UNA sola
        return PandoraResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)

    svc = _svc_con_salida_real(lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_ok)

    result = await svc.generate_animations()

    assert result["success"] is True
    assert _leer_output(activa) == "nuevo"
    assert _leer_output(inactiva) == "viejo", "se borró el candidato que Pandora no regeneró"


@pytest.mark.asyncio
async def test_lease_perdida_tras_run_exitoso_cierra_la_tx_sin_falso_positivo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    mock_journal: AsyncMock,
) -> None:
    """Un teardown fallido tras un run exitoso NO es un rollback fallido.

    ``rollback_completed`` también es False en la salida limpia —nunca hubo restore
    que hacer—, así que un ``LockLeaseLostError`` desde el ``__aexit__`` del lock
    dejaba la TX PENDING como si el restore hubiera reventado (review CodeRabbit
    #399). Es el mismo error de razonamiento del P1 de #397, cometido esta vez en
    el párrafo que explicaba por qué no podía pasar.
    """
    game, exe, salidas = _rutas_de_salida(tmp_path)
    for salida in salidas:
        _escribir_output(salida, "viejo")

    async def _corre_ok() -> PandoraResult:
        for salida in salidas:
            _escribir_output(salida, "nuevo")
        return PandoraResult(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)

    svc = _svc_con_salida_real(
        lock_manager, snapshot_manager, game=game, exe=exe, corrida=_corre_ok, journal=mock_journal
    )

    with patch("sky_claw.local.tools.pandora_service.SnapshotTransactionLock", _LeaseLostLock):
        result = await svc.generate_animations()

    assert result["success"] is False  # la exclusividad no estuvo garantizada
    # La TX se cierra: no hay salida parcial escondida que el recovery deba ver.
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(77)
    mock_journal.commit_transaction.assert_not_called()
    for salida in salidas:
        assert _leer_output(salida) == "nuevo", "no había restore que hacer; nada debió revertirse"
