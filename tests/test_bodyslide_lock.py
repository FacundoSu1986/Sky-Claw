"""Follow-up #3 — cerrar el vector ungated de BodySlide en la capa del agente.

Codex (#213) nombró el "mismo vector P1 de pandora/bodyslide": el tool del agente
corría el runner directo, sin el lock que serializa contra otros mutadores. Pandora
ya se cerró (#215); esto cierra BodySlide: ``run_bodyslide_batch`` serializa en el lock
``bodyslide-meshes`` cuando el lock está cableado, espejando ``run_loot_sort`` /
``run_pandora``. Sin lock manager (callers legacy / tests) se preserva la corrida directa.

Nota: hoy no hay Ritual de GUI que compita con BodySlide, así que la carrera es teórica;
esto cierra el patrón abierto de forma consistente.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.agent.tools.system_tools import (
    BODYSLIDE_MESHES_RESOURCE_ID,
    run_bodyslide_batch,
)
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager

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


@pytest.mark.asyncio
async def test_serializes_on_bodyslide_meshes_lock(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    seen: dict[str, object] = {}

    async def on_run(group: str, output_path: str) -> MagicMock:
        seen["info"] = await lock_manager.get_lock_info(BODYSLIDE_MESHES_RESOURCE_ID)
        return _runner_result()

    runner = MagicMock()
    runner.run_batch = AsyncMock(side_effect=on_run)

    out = json.loads(await run_bodyslide_batch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is True
    assert seen["info"] is not None  # lock tomado durante la corrida
    assert await lock_manager.get_lock_info(BODYSLIDE_MESHES_RESOURCE_ID) is None  # liberado al salir


@pytest.mark.asyncio
async def test_blocked_when_lock_already_held(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    await lock_manager.acquire_lock(BODYSLIDE_MESHES_RESOURCE_ID, "other", ttl=30.0)
    runner = MagicMock()
    runner.run_batch = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_bodyslide_batch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert "error" in out
    runner.run_batch.assert_not_awaited()  # serializado: no corrió


@pytest.mark.asyncio
async def test_direct_path_preserved_without_lock() -> None:
    runner = MagicMock()
    runner.run_batch = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_bodyslide_batch(runner, group="3BA", output_path="meshes"))

    assert out["success"] is True
    runner.run_batch.assert_awaited_once_with("3BA", "meshes")


@pytest.mark.asyncio
async def test_none_runner_is_structured_error() -> None:
    out = json.loads(await run_bodyslide_batch(None))
    assert "error" in out
