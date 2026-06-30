"""Follow-up de la review de Codex (#213, P1): cerrar el camino paralelo de Pandora.

El tool ``run_pandora`` del ``AsyncToolRegistry`` (capa LLM) corría
``PandoraRunner.run_pandora()`` directo, sin pasar por el lock ``behavior-graphs``
que sí toma el Ritual de la GUI → un agente podía competir con la GUI sobre los
mismos behavior graphs. Se enruta por ``PandoraPipelineService`` (que toma el lock),
espejando el patrón de ``run_loot_sort`` (Audit #190). Sin lock manager (callers
legacy / tests) se preserva la corrida directa.

También cubre el #3 (P2): ``GenerateAnimationsStrategy`` rechaza payload con claves
no honradas antes de pedir aprobación.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.agent.tools.system_tools import run_pandora
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator.tool_strategies.generate_animations import (
    GenerateAnimationsStrategy,
)
from sky_claw.local.tools.pandora_service import BEHAVIOR_GRAPHS_RESOURCE_ID

if TYPE_CHECKING:
    import pathlib


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


def _runner_result() -> MagicMock:
    return MagicMock(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)


# ── P1: el camino del agente serializa en el lock behavior-graphs ────────────────
@pytest.mark.asyncio
async def test_run_pandora_serializes_on_behavior_graphs_lock(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    seen: dict[str, object] = {}

    async def on_run() -> MagicMock:
        seen["info"] = await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID)
        return _runner_result()

    runner = MagicMock()
    runner.run_pandora = AsyncMock(side_effect=on_run)

    out = json.loads(await run_pandora(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is True
    assert seen["info"] is not None  # el lock se mantuvo durante la corrida
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None  # liberado al salir


@pytest.mark.asyncio
async def test_run_pandora_blocked_when_gui_ritual_holds_lock(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    # Simula el Ritual de la GUI corriendo: el lock está tomado.
    await lock_manager.acquire_lock(BEHAVIOR_GRAPHS_RESOURCE_ID, "gui-ritual", ttl=30.0)
    runner = MagicMock()
    runner.run_pandora = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_pandora(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    runner.run_pandora.assert_not_awaited()  # no corrió — serializado contra la GUI


@pytest.mark.asyncio
async def test_run_pandora_direct_path_preserved_without_lock() -> None:
    # Callers legacy / tests sin lock manager → corre directo (comportamiento previo).
    runner = MagicMock()
    runner.run_pandora = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_pandora(runner))

    assert out["success"] is True
    runner.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_pandora_none_runner_is_structured_error() -> None:
    out = json.loads(await run_pandora(None))
    assert "error" in out


# ── P2 (#3): aprobación no engañosa para generate_animations ─────────────────────
def test_generate_animations_validate_accepts_empty_payload() -> None:
    GenerateAnimationsStrategy(service=MagicMock()).validate_for_approval({})  # no raise


def test_generate_animations_validate_rejects_unexpected_keys() -> None:
    strat = GenerateAnimationsStrategy(service=MagicMock())
    with pytest.raises(ValueError) as exc:
        strat.validate_for_approval({"dry_run": True})
    assert "dry_run" in str(exc.value)


def test_generate_animations_describe_mentions_no_params() -> None:
    desc = GenerateAnimationsStrategy(service=MagicMock()).describe_for_approval({})
    assert isinstance(desc, str) and desc
