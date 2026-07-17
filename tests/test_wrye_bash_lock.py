"""§2.1 auditoría — Wrye Bash bajo el lock ``load-order`` + snapshot del Bashed Patch.

Era el ÚNICO mutador de plugins fuera de la disciplina lock+snapshot: el
Bashed Patch se construye leyendo el load order completo, así que un sort de
LOOT concurrente lo corrompía, y un timeout mataba a bash.py a mitad de
escritura dejando un ``Bashed Patch, 0.esp`` corrupto persistente (sin
rollback). Se cierran los DOS paths de producción:

- ``SupervisorAgent.execute_wrye_bash_pipeline`` (GUI/dispatcher).
- ``system_tools.generate_bashed_patch`` (registry del agente LLM/Telegram),
  espejando el patrón de ``run_bodyslide_batch`` (Codex #213): con lock
  cableado serializa; sin lock manager preserva la corrida directa.

Ambos serializan sobre el MISMO recurso que LOOT (``LOAD_ORDER_RESOURCE_ID``)
— el lock cross-process solo protege si todos los mutadores participan.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.agent.tools.system_tools import generate_bashed_patch
from sky_claw.antigravity.db.locks import DistributedLockManager, LockLeaseLostError
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator import supervisor as supervisor_mod
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME, WryeBashResult
from sky_claw.local.xedit.patch_orchestrator import DelegateToBashedPatch

if TYPE_CHECKING:
    import pathlib


class _LockLeaseLost:
    """Lock de frontera: en un clean-exit (el body no lanzó), ``__aexit__`` se
    comporta como una lease perdida — mismo contrato que ``SnapshotTransactionLock``
    real (review Codex #316: el heartbeat pudo perder el lease DURANTE un run
    largo aunque el body haya terminado con éxito)."""

    rollback_completed = False

    async def __aenter__(self) -> _LockLeaseLost:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: object, exc_tb: object) -> bool:
        if exc_type is None:
            raise LockLeaseLostError("lease perdida (simulado)")
        return False


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
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
    return FileSnapshotManager(snapshot_dir=d)


@pytest.fixture
def game_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "Skyrim"
    (d / "Data").mkdir(parents=True)
    return d


def _resultado(success: bool) -> WryeBashResult:
    return WryeBashResult(
        success=success,
        return_code=0 if success else 1,
        stdout="ok" if success else "",
        stderr="" if success else "boom",
        duration_seconds=1.0,
    )


def _runner(game_dir: pathlib.Path) -> MagicMock:
    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=game_dir)
    runner.generate_bashed_patch = AsyncMock(return_value=_resultado(True))
    return runner


def _supervisor(
    runner: MagicMock,
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> SupervisorAgent:
    """Supervisor sin __init__ (pesado): solo los colaboradores del pipeline.

    Mismo patrón de construcción que test_supervisor_dispatch_tool.py.
    """
    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.profile_name = "TestProfile"
    sup._wrye_bash_runner = runner  # _ensure_wrye_bash_runner lo devuelve tal cual
    sup._lock_manager = lock_manager
    sup.snapshot_manager = snapshot_manager
    return sup


# =============================================================================
# Pipeline del supervisor (GUI/dispatcher)
# =============================================================================


async def test_pipeline_serializa_en_el_lock_load_order(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    seen: dict[str, object] = {}
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        seen["info"] = await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID)
        return _resultado(True)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is True
    assert out["rolled_back"] is False
    assert seen["info"] is not None  # lock tomado DURANTE la generación
    assert await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None  # liberado


async def test_pipeline_bloqueado_si_loot_tiene_el_lock(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    """El MISMO recurso que LOOT: un sort en curso bloquea la generación."""
    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "loot-service", ttl=30.0)
    runner = _runner(game_dir)
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is False
    assert LOAD_ORDER_RESOURCE_ID in out["error"]
    runner.generate_bashed_patch.assert_not_awaited()  # serializado: no corrió


async def test_fallo_restaura_el_bashed_patch_previo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    """Timeout/fallo a mitad de escritura: el .esp previo vuelve byte a byte."""
    esp = game_dir / "Data" / BASHED_PATCH_NAME
    esp.write_bytes(b"TES4 patch previo OK")
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        esp.write_bytes(b"TES4 corrupto a medio esc")  # bash.py murió acá
        return _resultado(False)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is False
    assert out["rolled_back"] is True
    assert esp.read_bytes() == b"TES4 patch previo OK"  # snapshot restaurado


async def test_fallo_sin_esp_previo_no_declara_rollback(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    """Primera generación: no había nada que restaurar — rolled_back honesto."""
    runner = _runner(game_dir)
    runner.generate_bashed_patch = AsyncMock(return_value=_resultado(False))
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is False
    assert out["rolled_back"] is False


async def test_exito_no_revierte_el_esp_nuevo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    esp = game_dir / "Data" / BASHED_PATCH_NAME
    esp.write_bytes(b"TES4 patch viejo")
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        esp.write_bytes(b"TES4 patch nuevo")
        return _resultado(True)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is True
    assert esp.read_bytes() == b"TES4 patch nuevo"  # el snapshot NO pisa el éxito


# =============================================================================
# Handler del agente (registry LLM/Telegram)
# =============================================================================


async def test_handler_serializa_en_load_order(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    seen: dict[str, object] = {}
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        seen["info"] = await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID)
        return _resultado(True)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is True
    assert seen["info"] is not None
    assert await lock_manager.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None


async def test_handler_bloqueado_con_lock_tomado(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "other", ttl=30.0)
    runner = _runner(game_dir)

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert "error" in out
    runner.generate_bashed_patch.assert_not_awaited()


async def test_handler_fallo_restaura_el_esp_previo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    esp = game_dir / "Data" / BASHED_PATCH_NAME
    esp.write_bytes(b"TES4 previo")
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        esp.write_bytes(b"basura parcial")
        return _resultado(False)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert esp.read_bytes() == b"TES4 previo"


async def test_handler_path_directo_sin_lock_preservado(game_dir: pathlib.Path) -> None:
    runner = _runner(game_dir)

    out = json.loads(await generate_bashed_patch(runner))

    assert out["success"] is True
    runner.generate_bashed_patch.assert_awaited_once_with()


async def test_handler_runner_none_es_error_estructurado() -> None:
    out = json.loads(await generate_bashed_patch(None))
    assert "error" in out


# =============================================================================
# Lease perdida a mitad de run (review Codex #316) — el contrato dict/T11
# =============================================================================


async def test_pipeline_lease_perdida_devuelve_dict_no_propaga(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    """Si el heartbeat pierde el lease en un clean-exit, execute_wrye_bash_pipeline
    debe devolver el dict de error documentado — no dejar propagar LockLeaseLostError."""
    runner = _runner(game_dir)  # generate_bashed_patch() "termina con éxito"
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    with patch.object(supervisor_mod, "SnapshotTransactionLock", return_value=_LockLeaseLost()):
        out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is False
    assert "lease" in out["error"].lower()
    assert out["rolled_back"] is False


async def test_handler_lease_perdida_devuelve_json_de_error(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    runner = _runner(game_dir)

    # generate_bashed_patch hace el import de SnapshotTransactionLock LOCAL
    # (dentro de la función): el patch debe apuntar a la fuente
    # (sky_claw.antigravity.db.locks), no al módulo system_tools.
    with patch("sky_claw.antigravity.db.locks.SnapshotTransactionLock", return_value=_LockLeaseLost()):
        out = json.loads(
            await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager)
        )

    assert "error" in out
    assert "lease" in out["error"].lower()


# =============================================================================
# Primera generación fallida: sin snapshot previo, el .esp corrupto se limpia
# (review Codex #316 — antes quedaba persistente en Data/)
# =============================================================================


async def test_pipeline_primer_fallo_sin_esp_previo_borra_el_parcial(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    esp = game_dir / "Data" / BASHED_PATCH_NAME
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        esp.write_bytes(b"TES4 truncado a medio escribir")  # bash.py murió acá
        return _resultado(False)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)
    sup = _supervisor(runner, lock_manager, snapshot_manager)

    out = await sup.execute_wrye_bash_pipeline(validate_limit=False)

    assert out["success"] is False
    assert not esp.exists(), "el .esp corrupto de la primera generación no debe quedar persistente"


async def test_handler_primer_fallo_sin_esp_previo_borra_el_parcial(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game_dir: pathlib.Path,
) -> None:
    esp = game_dir / "Data" / BASHED_PATCH_NAME
    runner = _runner(game_dir)

    async def on_run() -> WryeBashResult:
        esp.write_bytes(b"TES4 truncado a medio escribir")
        return _resultado(False)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert not esp.exists()


# =============================================================================
# Anclas de sincronización
# =============================================================================


def test_nombre_canonico_del_bashed_patch_sincronizado() -> None:
    """El runner y la estrategia de delegación (ADR 0001) nombran el mismo .esp."""
    assert BASHED_PATCH_NAME == DelegateToBashedPatch.BASHED_PATCH_NAME
