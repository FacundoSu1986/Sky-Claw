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

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.db.locks import DistributedLockManager
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
